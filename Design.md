# Mini SE Workflow 设计文档

## 1. 设计目标与范围

本项目实现一个由需求 Agent、设计 Agent 和编码 Agent 组成的 Python 软件开发工作流。用户提供自然语言需求后，系统顺序整理需求、生成实现设计、生成代码并在隔离子进程中执行验证；失败时在限定次数内将错误反馈给模型修复。

设计关注点：

- **Workflow 编排**：注册并调度 Agent 和流程控制节点。
- **Agent 间通信**：通过共享运行上下文传递阶段产物。
- **流程控制**：条件分支、有限循环、并行独立分支。
- **错误处理与追踪**：执行结果判定、有限重试、运行日志和 JSONL 持久化。

当前实现是一个轻量 MVP。它提供节点级持久化检查点和 CLI 断点续跑，但不提供可视化编排或对任意生成代码的安全沙箱保证。

## 2. 总体架构与执行链

```text
用户 CLI
  │ 用户需求
  ▼
WorkflowEngine（按注册顺序调度 WorkflowNode）
  │
  ├─ RequirementAgent ── 写 requirement ─┐
  ├─ DesignAgent      ── 读 requirement，写 design
  ├─ CodingAgent      ── 读 requirement/design
  │    └─ LoopNode：生成 → 工具执行 → 反馈/修复 → 最终执行验证
  └─ ConditionalNode：依据 code_status 设置 workflow_outcome
  │
  ├─ WorkflowContext：运行期间共享的状态和日志
  ├─ JsonlCheckpointStore：节点成功后追加检查点到 output/workflow_checkpoints.jsonl
  └─ JsonlRunStore：运行结束或异常后追加快照到 output/workflow_runs.jsonl
```

需求、设计、编码阶段彼此依赖，因此主链按顺序运行。并行节点是可供其他独立工作流组合使用的控制结构，不会把当前主链中有依赖的 Agent 并行化。

## 3. 工作流节点与状态

### 3.1 节点抽象

`WorkflowNode` 定义统一的 `run(context)` 接口；`Agent` 是执行具体业务职责的节点。`WorkflowEngine.register(node)` 接受 Agent 和控制节点，并按注册顺序调度。引擎本身不依赖特定 Agent 的名称来判断编码失败。

### 3.2 共享状态与主要字段

`WorkflowContext` 在一次运行期间保存唯一 UUID `run_id`、字典状态和有序日志；检查点恢复会创建新上下文并恢复状态快照，但不会恢复取消标记。

| 阶段 | 读取 | 写入 |
|---|---|---|
| CLI | 用户需求 | `started_at`、`user_input`、最终 `workflow_outcome` |
| 需求 Agent | `user_input` | 带 `acceptance_criteria` 和 `assumptions` 的 schema 校验 `requirement` |
| 设计 Agent | `requirement` | 带有向无环 `tasks` 计划的 schema 校验 `design` |
| 编码 Agent | `requirement`、`design.tasks`、前次代码/错误 | `implementation_tasks`、`completed_tasks`、`code`、`code_status`、`output_file`、`coding_attempt` |
| 结果条件分支 | `code_status` | `workflow_outcome`（`success` 或 `failed`） |

编码状态最终为 `ok` 或 `failed`。工作流结果为 `success`、`success_with_recovery`、`success_with_warnings`、`failed` 或 `error`；如果工作流抛出异常，CLI 先将异常写入摘要和运行记录，再重新抛出异常。

所有运行都在 `WorkflowContext.issues` 中累积结构化问题项（类别、阶段、消息、尝试次数、是否可恢复、是否已恢复）；该字段与其余状态一起写入 JSONL。

## 4. 控制流节点

实现位于 `workflow/control.py`，三种节点均可作为工作流节点组合，也可嵌套在其他控制节点内。

### 4.1 条件分支：`ConditionalNode`

接受一个 `predicate(context)` 和真假两组节点。运行时求值一次，只执行选中的分支；未提供的分支为空操作。主流程在编码完成后以 `code_status == "ok"` 选择成功或失败结果节点。

### 4.2 循环：`LoopNode`

在每轮开始检查条件，并顺序执行循环体。`max_iterations` 必须大于零；当循环体执行达到上限后条件仍为真，会记录错误并抛出 `WorkflowControlError`，不会无限循环。

编码 Agent 使用此节点管理外层代码生成/修复尝试，最大次数由 `MAX_CODE_RETRIES` 提供。编码 Agent 的构造参数也可设置每次生成过程中的工具调用上限，默认 5 次；当前 CLI 未将其暴露为环境变量。

### 4.3 并行：`ParallelNode`

各分支内部顺序执行，分支之间由线程池并发执行。实现策略：

1. 深拷贝初始状态和日志，为每个分支创建隔离的 `WorkflowContext`。
2. 并发执行各分支，默认分支超时为 30 秒（`branch_timeout=None` 可关闭）；任一分支失败或超时则通知其他分支取消，并报告各分支结果，不合并任何分支状态。
3. 所有分支成功后比较状态差异并合并独立写入。
4. 若多个分支修改同一键且结果不一致，抛出 `WorkflowControlError`，不合并变更。

因使用深拷贝，状态中不可深拷贝的数据也会导致显式失败。线程不能被安全强杀，因此取消是协作式的：等待中的 future 会尝试取消，活动节点应周期性调用 `context.check_cancelled()`；宽限期结束仍未退出的分支会被标记为取消请求未及时响应，线程可能继续运行。并行只适用于互相独立的节点。

## 5. 编码、验证与错误处理

### 5.1 编码与需求验收循环

需求 Agent 产出测试可验证的 `acceptance_criteria` 和显式实现假设。设计 Agent 产出 1–8 项 `tasks`，每项包括稳定 ID、描述、依赖及验收标准。schema 校验器拒绝重复/非法 ID、未知/重复依赖、自依赖、循环依赖、空验收列表，并要求每条需求验收标准逐字出现在任务验收标准中；有效 DAG 按拓扑顺序执行。

编码 Agent 逐项实施任务。每次都要求模型返回完整、可运行的单文件实现，文件名由需求标题生成，并把上一任务代码和已完成任务交给模型；随后执行基本运行检查及至少 3 项任务验收测试。任务失败后只在该任务内按 `MAX_CODE_RETRIES` 重试；通过后才进入依赖顺序中的下一项。最后一项任务的验收测试使用完整需求标准，作为集成回归。仅当所有任务和最终回归通过后，才保存 `output/<run_id>/<需求标题>.py`、`output/<run_id>/test_solution.py` 并设置 `code_status=ok`。单次生成工具调用仍受 `max_tool_calls`（默认 5）限制。

基本运行检查、任务验收测试生成或执行失败都会在当前任务内触发修复重试；失败代码、测试结果和错误信息保存在 context 中供后续尝试修复。每个任务最多尝试 `MAX_CODE_RETRIES` 次（默认 3）；一个运行的编码生成总次数最多为任务数乘以该上限。需求或设计缺失时编码节点会将状态置为失败；任何任务耗尽重试时设置 `code_status=failed` 并停止后续任务。

### 5.2 可运行程序约定

生成提示要求模型返回保存后可直接运行的完整程序：

- 命令行或交互程序应提供清晰的 `main()` 启动入口，通常通过 `if __name__ == "__main__":` 调用。
- 函数/库型需求保留可导入接口，并提供简短的主入口示例。
- 验收测试使用标准库 `unittest`，应从测试文件同目录按生成的实现文件名加载模块，不等待人工输入、不依赖网络或第三方包，并覆盖需求中的正常行为、边界和无效输入（适用时）。
- 执行器将标准输入连接到 `DEVNULL`。交互代码读取输入时会收到 EOF，应处理 EOF 并正常退出。

验收测试由模型根据需求生成；测试通过并不构成形式化正确性证明，使用者仍应审阅需求理解和测试覆盖范围。

### 5.3 Python 执行器

`PythonExecutor` 将代码写入临时 `.py` 文件并通过当前 Python 解释器子进程执行，捕获 stdout/stderr，连接空标准输入，并在超时后终止进程。执行成功/失败以 `returncode == 0` 判定，即使没有输出，非零退出码也会报告失败。临时文件在执行后清理。

当前执行器默认超时为 15 秒。CLI 虽读取 `CODE_EXEC_TIMEOUT`，但目前没有将参数传入 `PythonExecutor`，因此更改该环境变量暂时不会改变实际超时。

### 5.4 LLM 与阶段错误

- `LLMClient` 对连接错误、限流和 API 错误进行有限重试，并使用指数等待。
- 需求和设计 Agent 使用 `workflow/nodes/structured_output.py` 中的 `TypedDict` 形状及运行时校验：字段必须齐全、不能多字段；文本字段必须为非空字符串，任务依赖必须组成有效 DAG，且需求标准须被任务覆盖。
- JSON 解析或 schema 校验失败时，Agent 将失败原因放入下一次提示并重试一次。仍失败时记录 `*_error`、设置 `*_degraded=true`，并使用结构有效的降级结果继续流程。
- LLM 调用异常会记录并立即使用降级结果；客户端本身已负责 API 错误重试。
- 编码阶段将代码生成、代码执行失败/超时/环境异常、验收测试生成失败/不通过/超时/环境异常、工具调用上限和重试耗尽分别分类记录。运行或验收问题进入外层重试；重试成功后此前可恢复 issue 标为已恢复，达到上限则追加 `retry_exhausted`。
- 编码任务按依赖顺序处理；中间任务的测试仅针对该任务验收标准，最后任务运行全量需求回归测试。实现目前逐步重生成完整单文件，而非逐任务编辑多个文件；测试可发现回归，但不保证模型完整保留早期实现。
- 控制节点错误向上抛出；CLI 记录工作流异常后重新抛出，不伪装成成功。

最终摘要区分普通成功、曾发生并已恢复错误的成功、仍有未恢复错误/降级的成功、重试失败及工作流异常，并逐项展示阶段、错误类别、尝试次数和恢复状态。降级设计用于尽量让流程继续，但可能降低输出质量；降级事件通过摘要、运行日志和 JSONL 追踪。

## 6. 日志与 JSONL 持久化

`WorkflowContext.log(node, action, message)` 在本次运行中追加日志三元组。CLI 在结束摘要中打印执行日志。

`JsonlRunStore` 将每次最终记录以 UTF-8 JSON Lines 追加到 `output/workflow_runs.jsonl`；`JsonlCheckpointStore` 在每个注册节点成功后追加快照到 `output/workflow_checkpoints.jsonl`。写入通过独立锁文件跨进程串行化，并在追加后 flush/fsync。运行记录和检查点共用同一 UUID `run_id`。

检查点包括：

- `run_id`、已完成节点索引和名称、工作流签名。
- `data`：共享状态快照。
- `logs`：截至该节点的日志。

最终运行记录包括：

- 唯一 `run_id`、开始和完成时间、最终 `outcome`。
- `state`：完整运行状态快照。
- `logs`：本次运行的节点日志。

CLI 输入 `resume <run_id>` 时读取该运行最新检查点，校验当前 Workflow 注册节点的类/名称签名，然后从最后一个完整检查点的下一个节点继续。若节点执行完毕但检查点尚未写入时进程中断，该节点可能重新执行；因此恢复语义是“从最近持久化节点继续”，不保证外部副作用恰好一次。签名不匹配、检查点结构损坏或找不到 run_id 时明确拒绝恢复。持久化失败会抛出 `WorkflowPersistenceError`。状态可能包含用户输入、代码及错误内容，需妥善保护。`output/` 默认由 Git 忽略。

## 7. 配置

| 配置 | 默认值 | 实际作用 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 无 | 必填 API Key |
| `DASHSCOPE_BASE_URL` | DashScope 兼容模式 URL | LLM API 地址 |
| `DASHSCOPE_MODEL` | `qwen-plus` | 模型名 |
| `MAX_CODE_RETRIES` | `3` | 编码外层最大尝试次数 |
| `ParallelNode.branch_timeout` | `30` 秒 | 每个并行分支的默认超时；设为 `None` 可关闭 |
| `CodingAgent.max_tool_calls` | `5` | 单次生成允许的工具调用数；构造参数可调整，CLI 未配置 |

## 8. 测试

运行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_workflow_control -v
```

测试使用模拟 Agent、LLM 和执行器，不需要真实 API Key。覆盖需求/设计 schema 校验、格式错误修正重试、降级状态与原因记录、结构化错误类别、错误恢复标记、执行环境分类与重试耗尽、条件分支、循环与上限、并行隔离/合并/冲突/超时/协作取消及分支失败汇总、检查点恢复和签名校验、运行 ID 产物隔离、并发 JSONL 追加、编码重试、验收测试生成和执行、测试失败重试、测试数量校验、可运行提示、退出码判定及交互式 EOF 行为。

## 9. 作业评审标准对照

| 评审维度 | 项目体现 |
|---|---|
| 流程完整性 | 需求→设计→编码端到端顺序执行；代码执行验证和有界修复重试 |
| Agent 协作 | `WorkflowContext` 在节点之间传递阶段产物 |
| 可扩展性 | `WorkflowNode`/`Agent` 抽象、注册机制和可组合控制节点 |
| 文档与追踪 | README、设计说明、运行日志、JSONL 状态快照及单元测试 |

## 10. 当前限制与后续方向

- `CODE_EXEC_TIMEOUT` 尚未从 CLI 接到执行器；工具调用上限目前没有环境变量配置。
- 子进程隔离和超时不是安全沙箱；不应对不可信代码视为安全。
- 模型降级路径可能继续执行但降低结果质量，需结合日志检查。
- 可继续加入基于依赖关系的图式工作流、可配置重试策略，以及更丰富的执行环境隔离。
