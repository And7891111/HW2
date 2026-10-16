# Mini SE Workflow

Mini SE 是一个基于 Python 的多 Agent 软件开发工作流示例。用户输入自然语言需求后，系统依次进行需求整理、设计生成和代码生成，并尝试执行验证；工作流提供有限循环、条件分支、并行节点、错误处理、执行日志和 JSON Lines 运行记录。

## 功能概览

- **端到端流程**：需求 Agent → 设计 Agent → 编码 Agent。
- **共享状态**：Agent 通过 `WorkflowContext` 传递需求、设计、代码和执行结果。
- **结构化需求分析**：需求 Agent 输出明确、可测试的验收标准和实现假设；设计 Agent 必须将每条标准映射到任务。
- **任务规划与增量编码**：设计 Agent 输出最多 8 个带依赖的实施任务；简单的单程序/单一用户流程默认作为一个整体实现，只有明确包含多个独立模块或交付物时才拆分；编码 Agent 按依赖顺序逐个实现、运行任务验收，最后用完整需求验收测试回归。
- **结构化输出校验**：需求、设计和任务计划按明确字段/类型 schema 校验；验证任务依赖引用、环路及需求标准覆盖。不合格时请求模型修正一次，仍失败则记录原因并使用标记过的降级结果。
- **错误分类与恢复摘要**：区分模型/结构化输出错误、代码执行/环境错误、验收失败和重试耗尽；错误恢复后标记已恢复，摘要及 JSONL 均保存 issue 分类。
- **可组合控制流**：支持条件分支 `ConditionalNode`、有限循环 `LoopNode` 和并行 `ParallelNode`。
- **需求验收**：为候选代码单独生成至少 3 个黑盒 `unittest` 验收用例；用户原始需求是验收范围的最高优先级依据，不得测试未确认的假设或私有实现细节；全部通过后才将编码标记为成功，并保存测试供复查。
- **编码修复**：基本运行检查或任务验收测试失败时，将错误反馈给模型，在每项任务 `MAX_CODE_RETRIES` 限制内重试；后续任务携带已通过任务和当前代码。
- **有界工具调用**：每次编码尝试最多调用代码执行工具 5 次；超过上限即结束本次生成并进入外层重试。
- **执行结果判定**：候选代码先进行语法检查，再由需求验收测试验证行为；独立执行工具仍以子进程退出码判定成功或失败，并提供关闭标准输入及超时保护。
- **可运行产物**：提示模型生成可直接运行的脚本入口；交互程序的自动验收使用模拟输入，不通过空 stdin 运行主流程来判定其行为。
- **运行追踪**：工作流动作日志在终端显示，并将每次运行的状态和日志追加到 `output/workflow_runs.jsonl`。
- **并行超时与协作取消**：分支超时或失败后取消尚未启动的任务，并通知运行中的分支在节点边界检查取消请求；超时和分支失败会汇总到错误信息。
- **节点检查点与恢复**：每个节点成功完成后追加运行检查点；进程中断后可输入 `resume <run_id>`，从最近成功节点之后继续。
- **运行隔离**：每次运行使用 UUID `run_id`，代码、验收测试和检查点都按运行 ID 隔离；JSONL 写入使用文件锁，避免并发追加互相穿插。

## 环境要求

- Python 3.10 或更新版本。
- DashScope API Key（运行主工作流需要）。

## 安装与配置

在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，至少填写 `DASHSCOPE_API_KEY`。也可以在你自己的虚拟环境中安装依赖；以上命令使用项目内 `.venv`。

```dotenv
DASHSCOPE_API_KEY=你的_API_Key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen-plus
MAX_CODE_RETRIES=3
CODE_EXEC_TIMEOUT=15
```

不要将真实 API Key 提交到版本控制。

## 运行工作流

```powershell
.\.venv\Scripts\python.exe .\main.py
```

按提示输入需求。输入 `exit` 或 `quit` 退出；每次提交的需求独立运行一次。实现文件名由需求标题生成（例如“数字猜谜游戏”会保存为 `数字猜谜游戏.py`），验收测试保存在 `output/<run_id>/test_solution.py`；终端摘要会给出具体路径。将摘要里的 UUID 和程序文件名复制到变量后，可单独运行程序或测试：

```powershell
$runId = "从执行摘要复制的 run_id"
$programFile = "从执行摘要复制的程序文件名.py"
.\.venv\Scripts\python.exe -m unittest discover -s ".\output\$runId" -p "test_solution.py" -v
```

也可以直接运行生成的程序：

```powershell
.\.venv\Scripts\python.exe ".\output\$runId\$programFile"
```

若上次运行因进程退出而中断，重新启动 CLI 后输入 `resume <run_id>`。检查点校验工作流结构签名；不匹配时会拒绝恢复。恢复从最后一个完整写入的成功节点之后开始，因此中断节点可能会重新执行。

交互式程序会在单独运行时等待用户输入；工作流内部的自动验证会将标准输入关闭，以免验证挂起。自动生成的验收测试能提高需求覆盖度，但仍需使用者审阅需求理解和测试覆盖是否充分。

## 高级流程控制

`WorkflowEngine.register()` 可注册 Agent 或任意 `WorkflowNode`。以下为使用形式示例，`success_agent`、`retry_agent` 和各并行 Agent 需由调用方实现：

```python
from workflow import ConditionalNode, LoopNode, ParallelNode

# 条件分支
engine.register(ConditionalNode(
    predicate=lambda context: context.get("code_status") == "ok",
    if_true=(success_agent,),
    if_false=(failure_agent,),
))

# 条件为真时执行循环体；最多执行 3 次。
# 达到上限后条件仍为真则抛出 WorkflowControlError。
engine.register(LoopNode(
    predicate=lambda context: context.get("needs_retry", False),
    body=(retry_agent,),
    max_iterations=3,
))

# 分支间并行，每个分支内部仍按顺序执行。
engine.register(ParallelNode(branches=(
    (independent_agent_a,),
    (independent_agent_b,),
)))
```

并行分支基于状态副本执行：全部成功后才合并变更；不同分支对同一状态键写入不同值会报冲突；任一分支失败或超时都不会合并分支状态。`branch_timeout` 默认 30 秒，可通过构造参数覆盖，设为 `None` 表示不设超时。失败/超时会尝试取消尚未启动的 future，并向活动分支的 `WorkflowContext` 发出取消标记。正在运行的 Python 线程无法强制终止；自定义长任务应周期性调用 `context.check_cancelled()`，否则取消只会在其返回或宽限期结束后被观察到。当前需求→设计→编码主链存在数据依赖，因此保持顺序执行。

主流程中的任务重试由 `LoopNode` 驱动，完成后由条件分支记录成功或失败结果。每个任务都需先通过自己验收标准的测试，最后一个任务执行完整需求回归测试。`CodingAgent` 的单次任务尝试工具调用上限当前固定为 5；可通过构造参数 `max_tool_calls` 调整该 Agent 的实例，但尚未提供环境变量配置。

设计任务最多 8 项，并在执行前检查依赖关系无环且有效、需求验收标准被任务逐字覆盖。当前版本每一步仍由模型生成完整单文件实现，文件名根据需求标题生成，并把上一版本作为上下文以逐步扩展，而不是修改多个源文件的补丁；复杂工程仍需人工审阅模块边界及验收测试覆盖。

## 执行错误处理

- LLM 客户端对连接错误、限流及 API 错误进行有限次数重试。
- 需求 Agent 或设计 Agent 出错时记录日志，并使用降级内容尝试继续流程。
- 需求 JSON 需包含可验证的 `acceptance_criteria` 和显式 `assumptions`；设计 JSON 需包含有向无环的 `tasks`，每项任务均有描述、依赖和验收标准。需求验收标准须逐字出现在至少一个任务验收标准中。格式/schema 错误会触发一次修正请求，最终降级原因保存在 `requirement_error`/`design_error`，并通过对应 `*_degraded` 状态标记。
- 最终摘要区分成功、成功且曾恢复、成功但仍有降级/未恢复警告、失败和异常中止；每条 issue 会显示阶段、类别、尝试次数和恢复状态。
- 编码 Agent 按依赖顺序逐项生成可运行代码、做基本运行检查并生成任务级验收测试；失败信息反馈给模型，同一任务最多重试 `MAX_CODE_RETRIES` 次。所有任务通过后，最后一项任务的完整需求回归测试覆盖全部需求验收标准。
- 只有至少 3 个验收用例实际运行且全部通过，编码 Agent 才会标记成功并保存按需求标题命名的实现文件及 `output/<run_id>/test_solution.py`。
- 每次编码尝试的工具调用数有限，防止模型持续发起工具调用。
- Python 执行器关闭标准输入、捕获标准输出和错误输出，并在超时后终止执行；成功与否以退出码为准。
- 控制流异常或工作流运行异常会记录到运行记录后重新抛出。检查点写入 `output/workflow_checkpoints.jsonl`；完整运行摘要写入 `output/workflow_runs.jsonl`。两个 JSONL 文件均使用锁保护并追加整行，恢复时选定 run_id 的最新检查点并校验流程签名。

## 执行日志与持久化记录

每次工作流运行完成或发生工作流异常后，程序向 `output/workflow_runs.jsonl` 追加一行 UTF-8 JSON。每行包含：

- `run_id`（同一运行的检查点与最终记录共用）、`started_at`、`completed_at`、`outcome`
- `state`：本次运行的共享状态快照
- `logs`：节点、动作和消息组成的执行日志

可用文本编辑器打开文件；每一行都是一条独立 JSON 记录。该快照可能含用户需求、生成代码或错误信息，请妥善保护。`workflow_checkpoints.jsonl` 每行保存 `run_id`、已完成节点索引/名称、流程签名、状态和日志。`output/` 在 `.gitignore` 中被忽略，运行记录、检查点和生成文件默认不会被 Git 跟踪。

## 项目结构

```text
main.py
requirements.txt
.env.example
README.md
Design.md
workflow/
├── base.py                 # WorkflowNode 和 Agent 抽象基类
├── context.py              # 共享运行状态与内存日志
├── control.py              # 条件、循环、并行控制节点
├── engine.py               # 按注册顺序调度节点
├── llm.py                  # OpenAI 兼容接口的 LLM 客户端
├── persistence.py          # JSONL 运行记录
├── nodes/
│   ├── requirement_agent.py
│   ├── design_agent.py
│   ├── coding_agent.py
│   └── structured_output.py # 需求/设计输出 schema 与校验
└── tools/
    └── python_executor.py  # 带超时的 Python 子进程执行器
tests/
└── test_workflow_control.py
output/                     # 生成代码和运行记录（Git 忽略）
```

## 配置项

| 配置 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 无 | DashScope API Key，运行主程序必填 |
| `DASHSCOPE_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容 API 地址 |
| `DASHSCOPE_MODEL` | `qwen-plus` | 模型名称 |
| `MAX_CODE_RETRIES` | `3` | 编码 Agent 外层最大尝试次数 |
| `CODE_EXEC_TIMEOUT` | `15` 秒 | 当前入口会读取该值，但尚未传入 `PythonExecutor`；执行器实际使用其默认 15 秒 |

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_workflow_control -v
```

测试覆盖条件分支、循环上限、并行执行和冲突处理、编码重试、退出码判定及 JSONL 持久化；测试不需要 DashScope API Key。
