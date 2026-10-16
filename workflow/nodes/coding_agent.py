"""编码 Agent：基于设计产出可运行代码，执行验证，失败自动修复重试。

这是"流程完整性 + 错误处理"的关键节点：
- 复用项目1的能力：LLM 写代码 → 执行 → 看报错 → 修复 → 再执行
- 带最大重试次数，超过后如实汇报失败原因
- 代码验证通过后保存到 output/ 目录
"""

import json
import os
import logging
from ..base import Agent, WorkflowNode
from ..context import WorkflowContext
from ..control import LoopNode
from ..tools.python_executor import PythonExecutor

logger = logging.getLogger(__name__)

MIN_ACCEPTANCE_TESTS = 3

OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "output",
)

# 编码Agent可用的工具（function calling schema）
CODING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": "只检查完整 Python 源码的语法，不执行程序，避免运行交互主流程或产生副作用。实际行为由独立需求验收测试验证。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "要执行的完整 Python 代码"}},
                "required": ["code"],
            },
        },
    }
]


class CodingAgent(Agent):
    """编码Agent：把设计变成通过需求验收测试的 Python 代码。

    状态约定（读取/写入 context）：
    - 读取 context.design
    - 写入 context.code（最终代码 str）
    - 写入 context.output_file（保存的文件名）
    - 写入 context.tests_file（验收测试文件名）
    - 写入 context.test_status（'passed' 或 'failed'）
    - 写入 context.code_status（'ok' 或 'failed'）
    """

    def __init__(self, llm_client, executor: PythonExecutor | None = None,
                 max_retries: int = 3, name: str = "编码Agent",
                 max_tool_calls: int = 5):
        if max_retries < 1:
            raise ValueError("max_retries 必须大于 0")
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls 必须大于 0")
        super().__init__(name)
        self.llm = llm_client
        self.executor = executor or PythonExecutor()
        self.max_retries = max_retries
        self.max_tool_calls = max_tool_calls

    def run(self, context: WorkflowContext) -> None:
        design = context.get("design", {})
        requirement = context.get("requirement", {})
        if not design:
            context.log(self.name, "错误", "没有设计方案，无法编码")
            context.record_issue(
                "missing_design",
                "编码",
                "没有可用设计方案，编码阶段无法开始",
            )
            context.set("code_status", "failed")
            return

        os.makedirs(self._run_output_dir(context), exist_ok=True)
        design_text = json.dumps(design, ensure_ascii=False, indent=2)
        req_text = json.dumps(requirement, ensure_ascii=False, indent=2)
        context.set(
            "solution_filename",
            self._solution_filename(requirement, context.get("user_input", "")),
        )
        tasks = self._implementation_tasks(
            design,
            requirement,
            context.get("user_input", ""),
        )
        context.set("implementation_tasks", tasks)
        context.set("implementation_task_index", 0)
        context.set("implementation_task_attempt", 0)
        context.set("completed_tasks", [])
        context.set("task_code", "")
        context.set("code_status", "retry")
        context.set("coding_attempt", 0)
        context.log(
            self.name,
            "开始",
            f"按任务计划逐项实现，共 {len(tasks)} 项",
        )
        loop = LoopNode(
            predicate=lambda state: (
                state.get("implementation_task_index", 0) < len(tasks)
                and state.get("implementation_task_attempt", 0) < self.max_retries
            ),
            body=(_CodingAttemptNode(self, design_text, req_text),),
            max_iterations=max(1, self.max_retries * len(tasks)),
            name=f"{self.name}重试循环",
        )
        loop.run(context)
        if context.get("implementation_task_index", 0) < len(tasks):
            context.set("code", "")
            context.set("code_status", "failed")
            task = tasks[context.get("implementation_task_index", 0)]
            context.record_issue(
                "retry_exhausted",
                "编码",
                f"任务 {task['id']} 在 {self.max_retries} 次尝试后仍未通过运行检查或验收",
                attempt=context.get("coding_attempt", 0),
            )
            context.log(
                self.name, "失败",
                f"任务 {task['id']} 重试耗尽，流程终止于本节点",
            )

    @staticmethod
    def _implementation_tasks(
        design: dict,
        requirement: dict,
        user_input: str = "",
    ) -> list[dict]:
        criteria = requirement.get("acceptance_criteria", [])
        if not isinstance(criteria, list) or not criteria:
            criteria = ["核心需求可通过验收测试验证"]

        if CodingAgent._is_simple_request(user_input):
            return [{
                "id": "implement-core",
                "title": "实现完整交互流程",
                "description": "完成用户明确要求的完整程序流程，不添加未提出的字段和规则。",
                "depends_on": [],
                "acceptance_criteria": criteria,
            }]

        tasks = design.get("tasks")
        if isinstance(tasks, list) and tasks:
            return tasks
        return [{
            "id": "implement-core",
            "title": "实现完整功能",
            "description": "按需求和设计实现完整、可运行的程序。",
            "depends_on": [],
            "acceptance_criteria": criteria,
        }]

    @staticmethod
    def _is_simple_request(user_input: str) -> bool:
        """Keep a short standalone request as one cohesive implementation task."""
        normalized = user_input.strip().casefold()
        if not normalized or len(normalized) > 120:
            return False
        explicit_components = (
            "多个模块", "多模块", "多个服务", "微服务", "前后端", "前端和后端",
            "数据库", "REST API", "REST接口", "部署", "认证系统", "权限系统",
            "多角色", "多个子系统", "multi-module", "microservice", "frontend",
            "backend", "database", "REST API", "deployment",
        )
        return not any(marker.casefold() in normalized for marker in explicit_components)

    def _produce_code(self, design_text: str, req_text: str, context, attempt: int) -> str:
        """让 LLM 生成或修复代码。首次用设计生成，后续带上前次错误。"""
        messages = [
            {"role": "system", "content": (
                "你是一名资深 Python 工程师。严格按照用户原始需求实现，不要擅自增加输入字段、"
                "业务规则、校验、重试策略或错误退出行为。根据需求与设计编写完整、可直接运行的 Python 程序。"
                "如果需求是命令行应用或交互式程序，提供清晰的提问流程和 main() 入口，并在 "
                "if __name__ == '__main__': 中调用它；不要假定需求要求处理 EOF、Ctrl+C 或特定退出码。"
                "如果需求是函数或库，则保留可导入接口，并在 main 入口提供简短示例。"
                "实现应有可导入的核心接口。每次写完代码必须调用 execute_python 工具进行语法检查；"
                "不要以无输入运行交互主流程来判断其行为。"
            )}
        ]

        user_content = (
            f"需求：\n{req_text}\n\n"
            f"用户原始需求（功能范围的最高优先级依据）：\n"
            f"{context.get('user_input', '')}\n\n"
            f"设计：\n{design_text}\n\n"
            f"请编写保存后可直接运行的完整 Python 程序，避免只定义函数却没有启动入口。"
            f"程序文件名应使用 `{context.get('solution_filename', 'generated_program.py')}`，"
            f"不要添加原始需求未提到的输入字段或业务校验。用 `execute_python` 检查语法后再回复。"
        )
        task = context.get("implementation_task", {})
        if task:
            previous_code = context.get("task_code", "")
            completed_tasks = context.get("completed_tasks", [])
            user_content += (
                "\n\n当前按计划逐项实现。请只推进当前任务，同时保留此前已完成的行为；"
                f"每次回复都必须是完整、可运行的 "
                f"{context.get('solution_filename', 'generated_program.py')}，而不是 diff 或代码片段。"
                f"\n已完成任务：{json.dumps(completed_tasks, ensure_ascii=False)}"
                f"\n当前任务：{json.dumps(task, ensure_ascii=False, indent=2)}"
            )
            if previous_code:
                user_content += f"\n\n上一任务/尝试的完整代码：\n```python\n{previous_code}\n```"

        # 非首次尝试：带上之前的失败信息
        if context.get("implementation_task_attempt", attempt) > 1:
            prev_code = context.get("last_code", "")
            prev_error = context.get("last_error", "")
            prev_tests = context.get("acceptance_tests", "")
            user_content += (
                f"\n\n上一版代码未通过运行检查或需求验收，请修复：\n"
                f"--- 代码 ---\n{prev_code}\n"
                f"--- 验收测试 ---\n{prev_tests}\n"
                f"--- 错误 ---\n{prev_error}"
            )

        messages.append({"role": "user", "content": user_content})

        try:
            # 执行多轮工具调用循环（模拟项目1的 ReAct 循环，但简化只跑代码）
            tool_call_count = 0
            while True:
                response = self.llm.chat(messages, tools=CODING_TOOLS)
                tool_calls = response.get("tool_calls")

                if not tool_calls:
                    # LLM 给出最终代码
                    final_code = self._extract_code(response.get("content", ""))
                    if final_code:
                        return final_code
                    return ""

                if tool_call_count + len(tool_calls) > self.max_tool_calls:
                    message = (
                        f"单次代码生成超过工具调用上限 "
                        f"{self.max_tool_calls}，已停止本次生成"
                    )
                    context.set("last_error", message)
                    context.record_issue(
                        "tool_call_limit",
                        "编码",
                        message,
                        attempt=context.get("coding_attempt"),
                        recoverable=True,
                    )
                    context.log(self.name, "限制", message)
                    return ""

                # 执行工具调用
                messages.append(response)
                for tc in tool_calls:
                    tool_call_count += 1
                    func_args = self._safe_args(tc["function"].get("arguments", ""))
                    code_to_run = func_args.get("code", "")
                    syntax_checker = getattr(
                        self.executor, "validate_syntax", None
                    )
                    if callable(syntax_checker):
                        result = syntax_checker(code_to_run)
                    else:
                        result = self.executor.run(code_to_run)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    if not result.startswith("执行成功"):
                        context.record_issue(
                            self._execution_issue_category(result),
                            "编码",
                            result,
                            attempt=context.get("coding_attempt"),
                            recoverable=True,
                        )
                    # 记录最近一次代码和结果，用于重试时喂给 LLM
                    context.set("last_code", code_to_run)
                    context.set("last_error", result)
        except Exception as exc:
            logger.exception("编码Agent LLM 调用失败")
            context.record_issue(
                "llm_call_failed",
                "编码",
                f"{type(exc).__name__}: {exc}",
                attempt=context.get("coding_attempt"),
                recoverable=True,
            )
            context.log(self.name, "失败", f"LLM 调用异常: {exc}")
            return ""

    def _produce_tests(
        self,
        design_text: str,
        req_text: str,
        code: str,
        context: WorkflowContext,
    ) -> str:
        """Generate standalone unittest acceptance tests for a candidate solution."""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一名独立的软件测试工程师。只编写 Python 标准库 unittest 测试文件，"
                    "测试文件将与候选实现文件放在同一目录并通过 `python -m unittest -v "
                    "test_solution` 执行。测试必须加载候选实现，不得复制或重写实现逻辑；"
                    f"至少包含 {MIN_ACCEPTANCE_TESTS} 个有意义的 test_ 用例，覆盖用户需求中明确的正常输入"
                    "与关键边界。测试应确定、快速、不依赖网络、人工输入或第三方包。"
                    "测试交互程序时使用 unittest.mock 模拟输入并捕获输出。只返回完整测试代码，"
                    "只验证用户原始需求明确要求的可观察行为；结构化需求中与原始需求不一致的额外"
                    "字段、校验规则或假设一律不是验收标准。不要臆造输入限制、重试次数、异常类型、"
                    "SystemExit/KeyboardInterrupt 行为。不要断言内部函数、内部配置、调用次数或实现细节，"
                    "除非用户明确要求这些接口。对于提问流程，用有限的模拟输入验证最终输出；不要要求"
                    "EOF、Ctrl+C 或特定异常处理行为，除非需求明确要求。"
                    "使用 importlib.util.spec_from_file_location 和 pathlib.Path 从测试文件同目录加载"
                    "指定的候选文件，避免依赖固定模块名。不要解释，也不要使用 Markdown 代码围栏。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"需求：\n{req_text}\n\n"
                    f"用户原始需求（验收范围的最高优先级依据）：\n"
                    f"{context.get('user_input', '')}\n\n"
                    f"设计：\n{design_text}\n\n"
                    f"当前实现任务：\n"
                    f"{json.dumps(context.get('implementation_task', {}), ensure_ascii=False, indent=2)}\n\n"
                    f"候选实现 {context.get('solution_filename', 'generated_program.py')}：\n```python\n{code}\n```\n\n"
                    "请依据需求设计黑盒验收测试，检查可观察的输入、输出和边界行为；"
                    "不要只验证函数存在或复刻候选实现。测试类须继承 unittest.TestCase，"
                    "文件末尾添加 `if __name__ == '__main__': unittest.main()`。"
                    f"候选实现文件名为 `{context.get('solution_filename', 'generated_program.py')}`。"
                ),
            },
        ]

        try:
            response = self.llm.chat(messages)
            test_code = self._extract_code(response.get("content", ""))
            if not test_code.strip():
                raise ValueError("模型未生成验收测试代码")
            return test_code
        except Exception as exc:
            logger.exception("验收测试生成失败")
            context.record_issue(
                "acceptance_test_generation_failed",
                "编码",
                f"{type(exc).__name__}: {exc}",
                attempt=context.get("coding_attempt"),
                recoverable=True,
            )
            context.log(self.name, "失败", f"验收测试生成异常: {exc}")
            return ""

    @staticmethod
    def _extract_code(content: str) -> str:
        """从 LLM 回复中提取代码（优先提取代码块）。"""
        if not content:
            return ""
        import re
        m = re.search(r"```(?:python)?\s*(.*?)\s*```", content, re.DOTALL)
        if m:
            return m.group(1)
        return content

    @staticmethod
    def _safe_args(raw) -> dict:
        import json
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_code(self, context, code: str) -> None:
        """把代码保存到当前运行独立的 output/<run_id>/ 目录。"""
        filename = context.get("solution_filename") or self._solution_filename(
            context.get("requirement", {}),
            context.get("user_input", ""),
        )
        relative_path = os.path.join(context.get("run_id"), filename)
        path = os.path.join(OUTPUT_DIR, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        context.set("output_file", relative_path)

    @staticmethod
    def _solution_filename(requirement: dict, user_input: str = "") -> str:
        """Create a readable Python filename from the requested feature title."""
        title = requirement.get("title", "")
        source = title.strip() if isinstance(title, str) else ""
        source = source or user_input.strip()
        filename = "".join(
            char if char.isalnum() or char == "_" else "_"
            for char in source
        ).strip("_")
        while "__" in filename:
            filename = filename.replace("__", "_")
        if not filename:
            filename = "generated_program"
        if filename[0].isdigit():
            filename = f"program_{filename}"
        reserved_names = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
        if filename.upper() in reserved_names:
            filename = f"program_{filename}"
        return f"{filename[:80]}.py"

    @staticmethod
    def _run_output_dir(context: WorkflowContext) -> str:
        return os.path.join(OUTPUT_DIR, context.get("run_id"))

    def _run_attempt(
        self,
        context: WorkflowContext,
        design_text: str,
        req_text: str,
    ) -> None:
        tasks = context.get("implementation_tasks", [])
        task_index = context.get("implementation_task_index", 0)
        task = tasks[task_index]
        task_attempt = context.get("implementation_task_attempt", 0) + 1
        context.set("implementation_task_attempt", task_attempt)
        context.set("implementation_task", task)

        attempt = context.get("coding_attempt", 0) + 1
        context.set("coding_attempt", attempt)
        context.log(
            self.name,
            "尝试",
            f"任务 {task_index + 1}/{len(tasks)} {task['id']}，"
            f"第 {task_attempt}/{self.max_retries} 次",
        )

        code = self._produce_code(design_text, req_text, context, attempt)
        if not code:
            error = context.get("last_error") or "未能生成代码"
            context.set("last_error", error)
            if not any(
                issue["stage"] == "编码"
                and issue["attempt"] == attempt
                and not issue["resolved"]
                for issue in context.issues
            ):
                context.record_issue(
                    "code_generation_failed",
                    "编码",
                    error,
                    attempt=attempt,
                    recoverable=True,
                )
            return

        syntax_checker = getattr(self.executor, "validate_syntax", None)
        if callable(syntax_checker):
            smoke_result = syntax_checker(code)
        else:
            smoke_result = self.executor.run(code)
        context.log(self.name, "语法检查", smoke_result[:200].replace("\n", " "))
        if not smoke_result.startswith("执行成功"):
            context.record_issue(
                self._execution_issue_category(smoke_result),
                "编码",
                smoke_result,
                attempt=attempt,
                recoverable=True,
            )
            context.set("test_status", "failed")
            context.set("last_code", code)
            context.set("last_error", smoke_result)
            context.log(self.name, "失败", "基本运行检查未通过，错误信息已回传，继续修复")
            context.set("task_code", code)
            return
        context.resolve_issues(
            "编码",
            {
                "code_execution_failed",
                "execution_timeout",
                "execution_environment_error",
                "llm_call_failed",
                "code_generation_failed",
                "tool_call_limit",
            },
        )

        is_final_task = task_index == len(tasks) - 1
        test_requirement = req_text
        if not is_final_task:
            requirement = json.loads(req_text)
            requirement["acceptance_criteria"] = task["acceptance_criteria"]
            test_requirement = json.dumps(requirement, ensure_ascii=False, indent=2)
        test_code = self._produce_tests(
            design_text,
            test_requirement,
            code,
            context,
        )
        if not test_code:
            failure = "验收测试生成失败，不能确认实现符合需求"
            if not any(
                issue["category"] == "acceptance_test_generation_failed"
                and issue["stage"] == "编码"
                and issue["attempt"] == attempt
                for issue in context.issues
            ):
                context.record_issue(
                    "acceptance_test_generation_failed",
                    "编码",
                    failure,
                    attempt=attempt,
                    recoverable=True,
                )
            context.set("test_status", "failed")
            context.set("last_code", code)
            context.set("last_error", failure)
            context.set("task_code", code)
            context.log(self.name, "失败", failure)
            return

        context.set("acceptance_tests", test_code)
        test_result = self.executor.run_tests(
            code,
            test_code,
            min_tests=MIN_ACCEPTANCE_TESTS,
            solution_filename=context.get(
                "solution_filename",
                "generated_program.py",
            ),
        )
        context.set("test_result", test_result)
        context.log(self.name, "验收测试", test_result[:300].replace("\n", " "))
        if test_result.startswith("验收测试通过"):
            context.resolve_issues("编码")
            if not is_final_task:
                completed = context.get("completed_tasks", [])
                completed.append({
                    "id": task["id"],
                    "title": task["title"],
                    "status": "passed",
                    "test_result": test_result,
                })
                context.set("completed_tasks", completed)
                context.set("task_code", code)
                context.set("implementation_task_index", task_index + 1)
                context.set("implementation_task_attempt", 0)
                context.set("test_status", "passed")
                context.log(
                    self.name,
                    "任务完成",
                    f"{task['id']} 验收通过，继续下一个任务",
                )
                return

            self._save_code(context, code)
            tests_filename = "test_solution.py"
            tests_path = os.path.join(
                self._run_output_dir(context),
                tests_filename,
            )
            os.makedirs(os.path.dirname(tests_path), exist_ok=True)
            with open(tests_path, "w", encoding="utf-8") as tests_file:
                tests_file.write(test_code)
            context.set("tests_file", tests_filename)
            context.set("test_status", "passed")
            context.set("code", code)
            context.set("code_status", "ok")
            completed = context.get("completed_tasks", [])
            completed.append({
                "id": task["id"],
                "title": task["title"],
                "status": "passed",
                "test_result": test_result,
            })
            context.set("completed_tasks", completed)
            context.set("implementation_task_index", task_index + 1)
            context.set("implementation_task_attempt", 0)
            context.log(
                self.name,
                "完成",
                f"需求验收通过，已保存代码和测试到 output/{context.get('output_file')} "
                f"及 output/{tests_filename}",
            )
            return

        context.set("last_code", code)
        context.set("task_code", code)
        context.set("test_status", "failed")
        context.set("last_error", test_result)
        context.record_issue(
            self._test_issue_category(test_result),
            "编码",
            test_result,
            attempt=attempt,
            recoverable=True,
        )
        context.log(self.name, "失败", "需求验收测试未通过，失败信息已回传，继续修复")

    @staticmethod
    def _execution_issue_category(result: str) -> str:
        if result.startswith("执行超时"):
            return "execution_timeout"
        if result.startswith("执行环境异常"):
            return "execution_environment_error"
        return "code_execution_failed"

    @staticmethod
    def _test_issue_category(result: str) -> str:
        if result.startswith("验收测试超时"):
            return "acceptance_test_timeout"
        if result.startswith("验收测试环境异常"):
            return "acceptance_test_environment_error"
        return "acceptance_test_failed"


class _CodingAttemptNode(WorkflowNode):
    """把一次编码尝试适配为通用循环节点。"""

    name = "单次编码尝试"

    def __init__(self, agent: CodingAgent, design_text: str, req_text: str):
        self.agent = agent
        self.design_text = design_text
        self.req_text = req_text

    def run(self, context: WorkflowContext) -> None:
        self.agent._run_attempt(context, self.design_text, self.req_text)
