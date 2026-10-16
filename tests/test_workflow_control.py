import json
import multiprocessing
import tempfile
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from workflow import (
    Agent,
    ConditionalNode,
    JsonlCheckpointStore,
    JsonlRunStore,
    LoopNode,
    ParallelNode,
    WorkflowContext,
    WorkflowControlError,
    WorkflowEngine,
    WorkflowPersistenceError,
)
from workflow.nodes.coding_agent import CodingAgent
from workflow.nodes.design_agent import DesignAgent
from workflow.nodes.requirement_agent import RequirementAgent
from workflow.nodes.structured_output import (
    StructuredOutputError,
    validate_design,
    validate_requirement,
)
from workflow.tools.python_executor import PythonExecutor


def _append_run_records(path, count):
    store = JsonlRunStore(path)
    for _ in range(count):
        store.append(WorkflowContext())


class SetValueAgent(Agent):
    def __init__(self, key, value):
        super().__init__(f"写入 {key}")
        self.key = key
        self.value = value

    def run(self, context):
        context.set(self.key, self.value)


class IncrementAgent(Agent):
    def run(self, context):
        context.set("count", context.get("count", 0) + 1)


class BarrierAgent(Agent):
    def __init__(self, barrier):
        super().__init__("同步")
        self.barrier = barrier

    def run(self, context):
        self.barrier.wait(timeout=2)


class RaiseAgent(Agent):
    def run(self, context):
        raise ValueError("branch failed")


class CooperativeWaitAgent(Agent):
    def __init__(self, barrier):
        super().__init__("协作等待")
        self.barrier = barrier

    def run(self, context):
        self.barrier.wait(timeout=2)
        while not context.cancellation_requested:
            time.sleep(0.005)
        context.check_cancelled()


class RaiseAfterBarrierAgent(Agent):
    def __init__(self, barrier):
        super().__init__("同步后失败")
        self.barrier = barrier

    def run(self, context):
        self.barrier.wait(timeout=2)
        raise ValueError("branch failed")


class SleepAgent(Agent):
    def __init__(self, delay):
        super().__init__("等待")
        self.delay = delay

    def run(self, context):
        time.sleep(self.delay)


class WorkflowControlTests(unittest.TestCase):
    def test_solution_filename_uses_a_safe_request_title(self):
        self.assertEqual(
            CodingAgent._solution_filename({"title": "数字猜谜游戏"}),
            "数字猜谜游戏.py",
        )
        self.assertEqual(
            CodingAgent._solution_filename({"title": "  3D Guess! "}),
            "program_3D_Guess.py",
        )
        self.assertEqual(
            CodingAgent._solution_filename({"title": "CON"}),
            "program_CON.py",
        )
        self.assertEqual(
            CodingAgent._solution_filename({}, "Build a task tracker"),
            "Build_a_task_tracker.py",
        )

    def test_short_standalone_request_is_not_over_decomposed(self):
        requirement = {
            "acceptance_criteria": [
                "程序逐项询问用户信息",
                "最后打印收集到的信息",
            ],
        }
        design = {
            "tasks": [
                {"id": f"task_{index}", "title": f"任务{index}"}
                for index in range(8)
            ],
        }

        tasks = CodingAgent._implementation_tasks(
            design,
            requirement,
            "生成一个信息调查记录，在程序向我提问，最后打印我的个人信息",
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(
            tasks[0]["acceptance_criteria"],
            requirement["acceptance_criteria"],
        )

    def test_explicit_multi_component_request_keeps_planned_tasks(self):
        tasks = [{"id": "api"}, {"id": "database"}]
        result = CodingAgent._implementation_tasks(
            {"tasks": tasks},
            {"acceptance_criteria": ["接口保存数据"]},
            "构建含 REST API 和数据库的后端服务",
        )
        self.assertEqual(result, tasks)

    def test_acceptance_test_prompt_prioritizes_original_request(self):
        class LLMStub:
            def __init__(self):
                self.messages = None

            def chat(self, messages, tools=None):
                self.messages = messages
                return {"content": "import unittest"}

        context = WorkflowContext()
        context.set(
            "user_input",
            "程序向用户提问并在最后打印回答，不要求捕获 Ctrl+C。",
        )
        llm = LLMStub()
        CodingAgent(llm)._produce_tests(
            "{}",
            '{"acceptance_criteria": ["打印回答", "捕获 Ctrl+C 后退出"]}',
            "def main(): pass",
            context,
        )

        system_prompt = llm.messages[0]["content"]
        user_prompt = llm.messages[1]["content"]
        self.assertIn("不要臆造", system_prompt)
        self.assertIn("不要求捕获 Ctrl+C", user_prompt)
        self.assertIn("最高优先级依据", user_prompt)

    def test_requirement_schema_requires_testable_acceptance_criteria(self):
        requirement = {
            "title": "问卷",
            "description": "收集用户回答",
            "inputs": [],
            "expected_output": "保存回答",
            "constraints": [],
            "acceptance_criteria": ["提交后返回结果"],
            "assumptions": [],
        }
        self.assertEqual(
            validate_requirement(requirement)["acceptance_criteria"],
            ["提交后返回结果"],
        )
        requirement["acceptance_criteria"] = []
        with self.assertRaisesRegex(StructuredOutputError, "至少需要一项"):
            validate_requirement(requirement)

    def test_design_tasks_are_topologically_ordered_and_cover_requirements(self):
        design = {
            "approach": "模块化",
            "structure": "模块",
            "function_signature": "def main()",
            "test_plan": "unittest",
            "impl_notes": [],
            "tasks": [
                {
                    "id": "api",
                    "title": "添加 API",
                    "description": "实现接口。",
                    "depends_on": ["core"],
                    "acceptance_criteria": ["接口返回数据"],
                },
                {
                    "id": "core",
                    "title": "实现核心逻辑",
                    "description": "实现业务计算。",
                    "depends_on": [],
                    "acceptance_criteria": ["核心计算结果正确"],
                },
            ],
        }
        result = validate_design(
            design,
            required_criteria=["接口返回数据", "核心计算结果正确"],
        )
        self.assertEqual(
            [task["id"] for task in result["tasks"]],
            ["core", "api"],
        )

    def test_design_rejects_cycles_and_uncovered_acceptance_criteria(self):
        task = {
            "id": "one",
            "title": "任务",
            "description": "实现功能。",
            "depends_on": ["one"],
            "acceptance_criteria": ["实现功能"],
        }
        design = {
            "approach": "直接实现",
            "structure": "单模块",
            "function_signature": "def main()",
            "test_plan": "unittest",
            "impl_notes": [],
            "tasks": [task],
        }
        with self.assertRaisesRegex(StructuredOutputError, "不能依赖自身"):
            validate_design(design)
        task["depends_on"] = []
        with self.assertRaisesRegex(StructuredOutputError, "未逐字覆盖"):
            validate_design(design, required_criteria=["核心输出正确"])

    def test_coding_agent_saves_each_run_to_a_separate_directory(self):
        first = WorkflowContext()
        second = WorkflowContext()
        agent = CodingAgent(None)

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("workflow.nodes.coding_agent.OUTPUT_DIR", output_dir):
                agent._save_code(first, "first run")
                agent._save_code(second, "second run")
                first_path = Path(output_dir) / first.get("output_file")
                second_path = Path(output_dir) / second.get("output_file")

            self.assertNotEqual(first.get("run_id"), second.get("run_id"))
            self.assertEqual(first_path.read_text(encoding="utf-8"), "first run")
            self.assertEqual(second_path.read_text(encoding="utf-8"), "second run")

    def test_requirement_agent_retries_invalid_schema_and_accepts_valid_json(self):
        valid_requirement = {
            "title": "数字猜谜",
            "description": "猜测随机数",
            "inputs": ["1 到 100 的猜测"],
            "expected_output": "提示猜测结果",
            "constraints": ["目标数在 1 到 100"],
            "acceptance_criteria": ["输入有效猜测后应提示猜测结果"],
            "assumptions": [],
        }

        class LLMStub:
            def __init__(self):
                self.responses = iter([
                    {"content": '{"title": "不完整"}'},
                    {"content": "```json\n" + json.dumps(valid_requirement, ensure_ascii=False) + "\n```"},
                ])
                self.messages = []

            def chat(self, messages, tools=None):
                self.messages.append(messages)
                return next(self.responses)

        llm = LLMStub()
        context = WorkflowContext()
        context.set("user_input", "生成数字猜谜游戏")

        RequirementAgent(llm).run(context)

        self.assertEqual(context.get("requirement"), valid_requirement)
        self.assertFalse(context.get("requirement_degraded"))
        self.assertEqual(context.get("requirement_error"), "")
        self.assertEqual(len(llm.messages), 2)
        self.assertIn("缺少字段", llm.messages[1][-1]["content"])
        self.assertTrue(any(action == "校验失败" for _, action, _ in context.logs))

    def test_requirement_agent_records_validation_failure_and_uses_valid_fallback(self):
        class LLMStub:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools=None):
                self.calls += 1
                return {"content": json.dumps({
                    "title": ["wrong type"],
                    "description": "需求描述",
                    "inputs": [],
                    "expected_output": "结果",
                    "constraints": [],
                    "acceptance_criteria": ["应提供核心行为"],
                    "assumptions": [],
                })}

        context = WorkflowContext()
        context.set("user_input", "制作问卷")

        RequirementAgent(LLMStub()).run(context)

        requirement = context.get("requirement")
        self.assertEqual(requirement["title"], "制作问卷")
        self.assertEqual(requirement["description"], "制作问卷")
        self.assertTrue(context.get("requirement_degraded"))
        self.assertIn("非空字符串", context.get("requirement_error"))
        self.assertTrue(any(action == "降级" for _, action, _ in context.logs))

    def test_design_agent_validates_fields_and_records_fallback_reason(self):
        valid_design = {
            "approach": "模块化实现",
            "structure": "单模块",
            "function_signature": "def main() -> None",
            "test_plan": "使用 unittest 测试边界",
            "impl_notes": ["处理无效输入"],
            "tasks": [{
                "id": "core",
                "title": "实现核心功能",
                "description": "完成核心业务逻辑。",
                "depends_on": [],
                "acceptance_criteria": ["核心功能能够正常工作"],
            }],
        }

        class LLMStub:
            def __init__(self, responses):
                self.responses = iter(responses)
                self.calls = 0

            def chat(self, messages, tools=None):
                self.calls += 1
                return next(self.responses)

        context = WorkflowContext()
        context.set("requirement", {
            "title": "测试需求",
            "acceptance_criteria": ["核心功能能够正常工作"],
        })
        llm = LLMStub([
            {"content": '{"approach": "只有一个字段"}'},
            {"content": json.dumps(valid_design, ensure_ascii=False)},
        ])

        DesignAgent(llm).run(context)

        self.assertEqual(context.get("design"), valid_design)
        self.assertFalse(context.get("design_degraded"))
        self.assertEqual(llm.calls, 2)

        context = WorkflowContext()
        context.set("requirement", {"title": "测试需求"})
        invalid_design = {
            "approach": 42,
            "structure": "单模块",
            "function_signature": "def main() -> None",
            "test_plan": "使用 unittest",
            "impl_notes": [],
            "tasks": [{
                "id": "core",
                "title": "实现核心功能",
                "description": "完成核心业务逻辑。",
                "depends_on": [],
                "acceptance_criteria": ["核心功能能够正常工作"],
            }],
        }
        llm = LLMStub([
            {"content": json.dumps(invalid_design)},
            {"content": json.dumps(invalid_design)},
        ])

        DesignAgent(llm).run(context)

        design = context.get("design")
        self.assertEqual(design["approach"], "按已校验的需求直接实现最小可运行方案")
        self.assertTrue(context.get("design_degraded"))
        self.assertIn("非空字符串", context.get("design_error"))
        self.assertTrue(any(action == "降级" for _, action, _ in context.logs))

    def test_conditional_executes_only_selected_branch(self):
        context = WorkflowContext()
        node = ConditionalNode(
            lambda state: state.get("enabled"),
            if_true=(SetValueAgent("selected", "true"),),
            if_false=(SetValueAgent("selected", "false"),),
        )

        node.run(context)

        self.assertEqual(context.get("selected"), "false")

    def test_loop_repeats_until_predicate_is_false(self):
        context = WorkflowContext()
        context.set("count", 0)
        node = LoopNode(
            lambda state: state.get("count") < 3,
            body=(IncrementAgent("increment"),),
            max_iterations=3,
        )

        node.run(context)

        self.assertEqual(context.get("count"), 3)

    def test_loop_raises_when_iteration_limit_is_exceeded(self):
        context = WorkflowContext()
        node = LoopNode(lambda state: True, body=(), max_iterations=2)

        with self.assertRaises(WorkflowControlError):
            node.run(context)

    def test_parallel_branches_merge_independent_writes(self):
        context = WorkflowContext()
        node = ParallelNode((
            (SetValueAgent("left", 1),),
            (SetValueAgent("right", 2),),
        ))

        node.run(context)

        self.assertEqual(context.get("left"), 1)
        self.assertEqual(context.get("right"), 2)

    def test_parallel_branches_run_concurrently(self):
        context = WorkflowContext()
        barrier = Barrier(2)
        node = ParallelNode((
            (BarrierAgent(barrier),),
            (BarrierAgent(barrier),),
        ))

        node.run(context)

        self.assertTrue(any(log[0] == "并行" for log in context.logs))

    def test_parallel_conflict_does_not_merge_partial_writes(self):
        context = WorkflowContext()
        context.set("value", 0)
        node = ParallelNode((
            (SetValueAgent("value", 1),),
            (SetValueAgent("value", 2),),
        ))

        with self.assertRaises(WorkflowControlError):
            node.run(context)

        self.assertEqual(context.get("value"), 0)

    def test_parallel_failure_does_not_merge_branch_writes(self):
        context = WorkflowContext()
        node = ParallelNode((
            (SetValueAgent("partial", True),),
            (RaiseAgent("raise"),),
        ))

        with self.assertRaises(WorkflowControlError):
            node.run(context)

        self.assertFalse(context.has("partial"))

    def test_parallel_timeout_reports_timed_out_branch(self):
        context = WorkflowContext()
        node = ParallelNode(
            ((SleepAgent(0.15),),),
            branch_timeout=0.02,
            cancellation_grace=0.01,
        )

        with self.assertRaisesRegex(WorkflowControlError, "超过分支超时"):
            node.run(context)

        self.assertEqual(context.issues[-1]["category"], "parallel_branch_failed")

    def test_parallel_failure_cooperatively_cancels_other_branch(self):
        context = WorkflowContext()
        barrier = Barrier(2)
        node = ParallelNode(
            (
                (RaiseAfterBarrierAgent(barrier),),
                (CooperativeWaitAgent(barrier),),
            ),
            branch_timeout=2,
            cancellation_grace=0.2,
        )

        with self.assertRaises(WorkflowControlError) as raised:
            node.run(context)

        self.assertIn("分支 1: ValueError: branch failed", str(raised.exception))
        self.assertIn("分支 2: 已协作取消", str(raised.exception))

    def test_engine_resumes_from_checkpoint_index(self):
        class CountingAgent(Agent):
            def __init__(self, name, key):
                super().__init__(name)
                self.key = key

            def run(self, context):
                context.set(self.key, context.get(self.key, 0) + 1)

        class FailOnceAgent(Agent):
            def __init__(self):
                super().__init__("second")
                self.attempts = 0

            def run(self, context):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("simulated interruption")
                context.set("second_count", context.get("second_count", 0) + 1)

        with tempfile.TemporaryDirectory() as directory:
            store = JsonlCheckpointStore(Path(directory) / "checkpoints.jsonl")
            engine = WorkflowEngine().register(
                CountingAgent("first", "first_count")
            ).register(FailOnceAgent())
            context = WorkflowContext()
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                engine.run(
                    context,
                    on_node_complete=lambda state, index, node: store.save(
                        state, index, node.name, engine.signature
                    ),
                )
            restored, completed_index = store.load_latest(
                context.get("run_id"), engine.signature
            )
            engine.run(restored, start_index=completed_index)

        self.assertEqual(completed_index, 1)
        self.assertEqual(restored.get("first_count"), 1)
        self.assertEqual(restored.get("second_count"), 1)

    def test_checkpoint_rejects_different_workflow_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlCheckpointStore(Path(directory) / "checkpoints.jsonl")
            context = WorkflowContext()
            store.save(context, 1, "node", "expected")

            with self.assertRaisesRegex(WorkflowPersistenceError, "不匹配"):
                store.load_latest(context.get("run_id"), "different")

    def test_engine_registers_and_runs_control_nodes(self):
        context = WorkflowContext()
        engine = WorkflowEngine().register(ConditionalNode(
            lambda state: True,
            if_true=(SetValueAgent("done", True),),
        ))

        engine.run(context)

        self.assertTrue(context.get("done"))

    def test_coding_agent_uses_loop_node_for_retries(self):
        class LLMStub:
            def chat(self, messages, tools=None):
                if "测试文件" in messages[0]["content"]:
                    return {"content": (
                        "import unittest\n"
                        "import solution\n"
                        "class AcceptanceTests(unittest.TestCase):\n"
                        "    def test_module_loads(self):\n"
                        "        self.assertTrue(hasattr(solution, 'main'))\n"
                        "    def test_main_is_callable(self):\n"
                        "        self.assertTrue(callable(solution.main))\n"
                        "    def test_module_has_expected_name(self):\n"
                        "        self.assertEqual(solution.__name__, 'solution')\n"
                        "if __name__ == '__main__':\n"
                        "    unittest.main()\n"
                    )}
                return {"content": "def main():\n    print('ok')\n"}

        class ExecutorStub:
            def __init__(self):
                self.calls = 0
                self.test_calls = 0

            def run(self, code):
                self.calls += 1
                if self.calls == 1:
                    return "执行失败（退出码 1）\n\n[错误输出]\nassertion error"
                return "执行成功\n\n[标准输出]\nok"

            def run_tests(
                self,
                solution_code,
                test_code,
                min_tests=1,
                solution_filename="solution.py",
            ):
                self.test_calls += 1
                return "验收测试通过（3 个用例）\n\nRan 3 tests"

        context = WorkflowContext()
        context.set("requirement", {"title": "test"})
        context.set("design", {"approach": "test"})
        executor = ExecutorStub()
        with tempfile.TemporaryDirectory() as output_dir:
            with patch("workflow.nodes.coding_agent.OUTPUT_DIR", output_dir):
                CodingAgent(
                    LLMStub(), executor=executor, max_retries=2
                ).run(context)
                run_output_dir = Path(output_dir) / context.get("run_id")
                output_file = run_output_dir / "test.py"
                self.assertTrue(output_file.is_file())
                self.assertTrue((run_output_dir / "test_solution.py").is_file())
                self.assertEqual(
                    context.get("output_file"),
                    str(Path(context.get("run_id")) / "test.py"),
                )

        self.assertEqual(context.get("coding_attempt"), 2)
        self.assertEqual(context.get("code_status"), "ok")
        self.assertEqual(context.get("test_status"), "passed")
        self.assertEqual(executor.calls, 2)
        self.assertEqual(executor.test_calls, 1)
        execution_issue = next(
            issue for issue in context.issues
            if issue["category"] == "code_execution_failed"
        )
        self.assertTrue(execution_issue["resolved"])

    def test_coding_agent_implements_and_tests_plan_tasks_incrementally(self):
        class LLMStub:
            def __init__(self):
                self.code_prompts = []
                self.test_prompts = []
                self.code_index = 0

            def chat(self, messages, tools=None):
                if "测试文件" in messages[0]["content"]:
                    self.test_prompts.append(messages[-1]["content"])
                    return {
                        "content": (
                            "import unittest\n"
                            "class AcceptanceTests(unittest.TestCase):\n"
                            "    def test_behavior(self):\n"
                            "        self.assertTrue(True)\n"
                            "    def test_boundary(self):\n"
                            "        self.assertEqual(1, 1)\n"
                            "    def test_invalid(self):\n"
                            "        self.assertIsNone(None)\n"
                        )
                    }
                self.code_prompts.append(messages[-1]["content"])
                self.code_index += 1
                return {"content": f"def main():\n    return {self.code_index}\n"}

        class ExecutorStub:
            def __init__(self):
                self.test_calls = 0
                self.test_results = iter([
                    "验收测试通过（3 个用例）",
                    "验收测试失败（退出码 1）：接口不符合预期",
                    "验收测试通过（3 个用例）",
                ])

            def run(self, code):
                return "执行成功"

            def run_tests(
                self,
                solution_code,
                test_code,
                min_tests=1,
                solution_filename="solution.py",
            ):
                self.test_calls += 1
                return next(self.test_results)

        requirement = {
            "title": "多阶段应用",
            "acceptance_criteria": [
                "核心计算返回正确结果",
                "接口返回核心计算结果",
            ],
        }
        design = {
            "approach": "先实现核心逻辑，再提供接口",
            "tasks": [
                {
                    "id": "core",
                    "title": "核心计算",
                    "description": "实现核心计算逻辑。",
                    "depends_on": [],
                    "acceptance_criteria": ["核心计算返回正确结果"],
                },
                {
                    "id": "api",
                    "title": "接口",
                    "description": "通过接口暴露核心计算。",
                    "depends_on": ["core"],
                    "acceptance_criteria": ["接口返回核心计算结果"],
                },
            ],
        }
        llm = LLMStub()
        executor = ExecutorStub()
        context = WorkflowContext()
        context.set("requirement", requirement)
        context.set("design", design)

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("workflow.nodes.coding_agent.OUTPUT_DIR", output_dir):
                CodingAgent(llm, executor=executor, max_retries=2).run(context)
                saved_code = (
                    Path(output_dir) / context.get("output_file")
                ).read_text(encoding="utf-8")

        self.assertEqual(context.get("code_status"), "ok")
        self.assertEqual(context.get("implementation_task_index"), 2)
        self.assertEqual(
            [task["id"] for task in context.get("completed_tasks")],
            ["core", "api"],
        )
        self.assertEqual(executor.test_calls, 3)
        self.assertIn("核心计算返回正确结果", llm.test_prompts[0])
        self.assertNotIn(
            "接口返回核心计算结果",
            llm.test_prompts[0].split("设计：", maxsplit=1)[0],
        )
        self.assertIn("接口返回核心计算结果", llm.test_prompts[1])
        self.assertIn("def main():\n    return 2", llm.code_prompts[2])
        self.assertEqual(saved_code, "def main():\n    return 3\n")

    def test_coding_agent_records_retry_exhaustion_and_execution_category(self):
        class LLMStub:
            def chat(self, messages, tools=None):
                return {"content": "print('candidate')"}

        class ExecutorStub:
            def run(self, code):
                return "执行环境异常：OSError: interpreter unavailable"

        context = WorkflowContext()
        context.set("requirement", {"title": "test"})
        context.set("design", {"approach": "test"})

        CodingAgent(LLMStub(), executor=ExecutorStub(), max_retries=1).run(context)

        categories = [issue["category"] for issue in context.issues]
        self.assertIn("execution_environment_error", categories)
        self.assertIn("retry_exhausted", categories)
        self.assertEqual(context.get("code_status"), "failed")
        environment_issue = next(
            issue for issue in context.issues
            if issue["category"] == "execution_environment_error"
        )
        self.assertFalse(environment_issue["resolved"])
        self.assertEqual(environment_issue["attempt"], 1)

    def test_execution_failures_are_classified_by_failure_kind(self):
        self.assertEqual(
            CodingAgent._execution_issue_category("执行超时：超过 2 秒"),
            "execution_timeout",
        )
        self.assertEqual(
            CodingAgent._execution_issue_category("执行环境异常：OSError"),
            "execution_environment_error",
        )
        self.assertEqual(
            CodingAgent._test_issue_category("验收测试超时：超过 2 秒"),
            "acceptance_test_timeout",
        )
        self.assertEqual(
            CodingAgent._test_issue_category("验收测试环境异常：OSError"),
            "acceptance_test_environment_error",
        )

    def test_coding_agent_requests_a_directly_runnable_program(self):
        class LLMStub:
            def __init__(self):
                self.messages = None

            def chat(self, messages, tools=None):
                self.messages = messages
                return {"content": "print('ready')"}

        llm = LLMStub()
        agent = CodingAgent(llm)
        context = WorkflowContext()
        context.set("solution_filename", "guess_number_game.py")

        code = agent._produce_code("{}", "{}", context, 1)

        prompt = llm.messages[0]["content"]
        user_prompt = llm.messages[1]["content"]
        self.assertIn("if __name__ == '__main__'", prompt)
        self.assertIn("EOF", prompt)
        self.assertIn("可直接运行", prompt)
        self.assertIn("guess_number_game.py", user_prompt)
        self.assertEqual(code, "print('ready')")

    def test_interactive_code_tool_checks_syntax_without_running_main(self):
        class LLMStub:
            def __init__(self):
                self.calls = 0
                self.tool_result = ""

            def chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return {"tool_calls": [{
                        "id": "syntax-check",
                        "function": {
                            "arguments": json.dumps({
                                "code": "name = input('Name: ')\nprint(name)",
                            }),
                        },
                    }]}
                self.tool_result = messages[-1]["content"]
                return {"content": "print('ready')"}

        llm = LLMStub()
        agent = CodingAgent(llm, executor=PythonExecutor(timeout=2))

        code = agent._produce_code("{}", "{}", WorkflowContext(), 1)

        self.assertEqual(code, "print('ready')")
        self.assertIn("语法检查通过", llm.tool_result)
        self.assertNotIn("EOFError", llm.tool_result)

    def test_acceptance_test_failure_retries_instead_of_marking_success(self):
        class LLMStub:
            def __init__(self):
                self.code_calls = 0
                self.test_calls = 0

            def chat(self, messages, tools=None):
                if "测试文件" in messages[0]["content"]:
                    self.test_calls += 1
                    return {"content": (
                        "import unittest\n"
                        "class AcceptanceTests(unittest.TestCase):\n"
                        "    def test_requirement(self):\n"
                        "        self.assertTrue(False)\n"
                        "    def test_boundary(self):\n"
                        "        self.assertTrue(False)\n"
                        "    def test_invalid_input(self):\n"
                        "        self.assertTrue(False)\n"
                    )}
                self.code_calls += 1
                return {"content": "print('runs')"}

        class ExecutorStub:
            def __init__(self):
                self.test_calls = 0

            def run(self, code):
                return "执行成功"

            def run_tests(
                self,
                solution_code,
                test_code,
                min_tests=1,
                solution_filename="solution.py",
            ):
                self.test_calls += 1
                if self.test_calls == 1:
                    return "验收测试失败（退出码 1）\nAssertionError"
                return "验收测试通过（3 个用例）\nRan 3 tests"

        llm = LLMStub()
        executor = ExecutorStub()
        context = WorkflowContext()
        context.set("requirement", {"title": "test"})
        context.set("design", {"approach": "test"})

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("workflow.nodes.coding_agent.OUTPUT_DIR", output_dir):
                CodingAgent(llm, executor=executor, max_retries=2).run(context)
                run_output_dir = Path(output_dir) / context.get("run_id")
                self.assertTrue((run_output_dir / "test.py").is_file())
                self.assertTrue((run_output_dir / "test_solution.py").is_file())

        self.assertEqual(context.get("coding_attempt"), 2)
        self.assertEqual(context.get("code_status"), "ok")
        self.assertEqual(executor.test_calls, 2)
        self.assertEqual(llm.code_calls, 2)
        self.assertEqual(llm.test_calls, 2)
        acceptance_issue = next(
            issue for issue in context.issues
            if issue["category"] == "acceptance_test_failed"
        )
        self.assertTrue(acceptance_issue["resolved"])

    def test_executor_runs_interactive_program_with_closed_stdin(self):
        result = PythonExecutor(timeout=2).run(
            "try:\n"
            "    input('输入内容: ')\n"
            "except EOFError:\n"
            "    print('正常退出')\n"
        )

        self.assertTrue(result.startswith("执行成功"), result)
        self.assertIn("正常退出", result)

    def test_executor_validates_syntax_without_running_program(self):
        result = PythonExecutor.validate_syntax("raise RuntimeError('must not run')")
        self.assertTrue(result.startswith("执行成功"), result)

        invalid_result = PythonExecutor.validate_syntax("def broken(:\n    pass\n")
        self.assertTrue(invalid_result.startswith("语法检查失败"), invalid_result)

    def test_executor_reports_nonzero_exit_without_output_as_failure(self):
        result = PythonExecutor(timeout=2).run("raise SystemExit(7)")

        self.assertTrue(result.startswith("执行失败（退出码 7）"), result)
        self.assertIn("无输出", result)

    def test_executor_reports_zero_exit_without_output_as_success(self):
        result = PythonExecutor(timeout=2).run("pass")

        self.assertTrue(result.startswith("执行成功"), result)
        self.assertIn("无输出", result)

    def test_executor_runs_acceptance_tests_against_solution_module(self):
        result = PythonExecutor(timeout=2).run_tests(
            "def add(left, right):\n"
            "    return left + right\n",
            "import unittest\n"
            "import solution\n"
            "class AcceptanceTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(solution.add(2, 3), 5)\n"
            "    def test_add_zero(self):\n"
            "        self.assertEqual(solution.add(0, 0), 0)\n"
            "    def test_add_negative(self):\n"
            "        self.assertEqual(solution.add(-2, 3), 1)\n",
        )

        self.assertTrue(result.startswith("验收测试通过（3 个用例）"), result)

    def test_executor_runs_tests_against_a_requirement_named_module(self):
        test_code = (
            "import importlib.util\n"
            "import unittest\n"
            "from pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'candidate', Path(__file__).with_name('数字猜谜游戏.py'))\n"
            "candidate = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(candidate)\n"
            "class AcceptanceTests(unittest.TestCase):\n"
            "    def test_guess_one(self):\n"
            "        self.assertEqual(candidate.guess(42), '正确')\n"
            "    def test_guess_two(self):\n"
            "        self.assertEqual(candidate.guess(41), '太小')\n"
            "    def test_guess_three(self):\n"
            "        self.assertEqual(candidate.guess(43), '太大')\n"
        )
        result = PythonExecutor(timeout=2).run_tests(
            "def guess(value):\n"
            "    if value == 42:\n"
            "        return '正确'\n"
            "    return '太小' if value < 42 else '太大'\n",
            test_code,
            min_tests=3,
            solution_filename="数字猜谜游戏.py",
        )

        self.assertTrue(result.startswith("验收测试通过（3 个用例）"), result)

    def test_executor_rejects_acceptance_test_files_without_test_cases(self):
        result = PythonExecutor(timeout=2).run_tests(
            "value = 1\n",
            "import unittest\n"
            "class AcceptanceTests(unittest.TestCase):\n"
            "    pass\n",
        )

        self.assertTrue(result.startswith("验收测试失败：仅运行 0 个用例"), result)

    def test_executor_rejects_acceptance_test_suites_below_minimum_size(self):
        result = PythonExecutor(timeout=2).run_tests(
            "value = 1\n",
            "import unittest\n"
            "class AcceptanceTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(1, 1)\n",
            min_tests=3,
        )

        self.assertTrue(result.startswith("验收测试失败：仅运行 1 个用例"), result)

    def test_coding_agent_stops_after_maximum_tool_calls(self):
        class LLMStub:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools=None):
                self.calls += 1
                return {
                    "tool_calls": [{
                        "id": f"call-{self.calls}",
                        "function": {
                            "arguments": '{"code": "print(1)"}',
                        },
                    }]
                }

        class ExecutorStub:
            def __init__(self):
                self.calls = 0

            def run(self, code):
                self.calls += 1
                return "执行成功\n\n[标准输出]\n1"

        llm = LLMStub()
        executor = ExecutorStub()
        agent = CodingAgent(llm, executor=executor, max_tool_calls=2)
        context = WorkflowContext()

        code = agent._produce_code("{}", "{}", context, 1)

        self.assertEqual(code, "")
        self.assertEqual(llm.calls, 3)
        self.assertEqual(executor.calls, 2)
        self.assertIn("超过工具调用上限 2", context.get("last_error"))
        self.assertTrue(any(action == "限制" for _, action, _ in context.logs))

    def test_jsonl_store_appends_a_run_snapshot(self):
        context = WorkflowContext()
        context.set("started_at", "2026-10-09T00:00:00+00:00")
        context.set("workflow_outcome", "success")
        context.set("message", "运行完成")
        context.log("测试节点", "完成", "状态已保存")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            store = JsonlRunStore(str(path))
            first = store.append(context)
            second = store.append(context)
            lines = path.read_text(encoding="utf-8").splitlines()
            persisted_first, persisted_second = map(json.loads, lines)

        self.assertEqual(len(lines), 2)
        self.assertEqual(persisted_first["run_id"], first["run_id"])
        self.assertEqual(persisted_second["run_id"], second["run_id"])
        self.assertEqual(persisted_first["logs"], [["测试节点", "完成", "状态已保存"]])
        self.assertEqual(persisted_first["outcome"], "success")
        self.assertEqual(persisted_first["state"]["message"], "运行完成")

    def test_jsonl_store_surfaces_unserializable_state(self):
        context = WorkflowContext()
        context.set("not_json", object())

        with tempfile.TemporaryDirectory() as directory:
            store = JsonlRunStore(str(Path(directory) / "runs.jsonl"))
            with self.assertRaises(WorkflowPersistenceError):
                store.append(context)

    def test_jsonl_store_serializes_concurrent_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            store = JsonlRunStore(path)
            contexts = [WorkflowContext() for _ in range(24)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(store.append, contexts))

            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(records), len(contexts))
        self.assertEqual(
            {record["run_id"] for record in records},
            {context.get("run_id") for context in contexts},
        )

    def test_jsonl_store_serializes_cross_process_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "runs.jsonl")
            spawn = multiprocessing.get_context("spawn")
            processes = [
                spawn.Process(target=_append_run_records, args=(path, 4))
                for _ in range(3)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)

            self.assertTrue(all(not process.is_alive() for process in processes))
            self.assertEqual([process.exitcode for process in processes], [0, 0, 0])
            records = [
                json.loads(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(records), 12)


if __name__ == "__main__":
    unittest.main()
