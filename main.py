import os
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("ALL_PROXY", None)

import os
import sys
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workflow import (
    WorkflowEngine,
    WorkflowContext,
    ConditionalNode,
    LLMClient,
    Agent,
    JsonlRunStore,
    JsonlCheckpointStore,
    RequirementAgent,
    DesignAgent,
    CodingAgent,
    WorkflowPersistenceError,
)


class _WorkflowOutcomeAgent(Agent):
    def __init__(self, outcome: str):
        super().__init__(f"{outcome}结果")
        self.outcome = outcome

    def run(self, context: WorkflowContext) -> None:
        outcome = self.outcome
        if outcome == "success" and context.issues:
            if any(not issue["resolved"] for issue in context.issues):
                outcome = "success_with_warnings"
            else:
                outcome = "success_with_recovery"
        context.set("workflow_outcome", outcome)
        context.log(self.name, "完成", f"工作流结果: {outcome}")


_OUTCOME_LABELS = {
    "success": "成功",
    "success_with_recovery": "成功（发生过可恢复错误，已修复）",
    "success_with_warnings": "成功（存在未恢复错误或降级结果）",
    "failed": "失败（未通过编码/验收）",
    "error": "异常中止",
}

_ISSUE_LABELS = {
    "missing_input": "缺少需求输入",
    "missing_requirement": "缺少需求文档",
    "structured_output_invalid": "结构化输出校验失败",
    "stage_degraded": "阶段降级",
    "llm_call_failed": "模型调用失败",
    "missing_design": "缺少设计",
    "code_generation_failed": "代码生成失败",
    "code_execution_failed": "代码执行失败",
    "execution_timeout": "代码执行超时",
    "execution_environment_error": "代码执行环境异常",
    "acceptance_test_generation_failed": "验收测试生成失败",
    "acceptance_test_failed": "验收测试未通过",
    "acceptance_test_timeout": "验收测试超时",
    "acceptance_test_environment_error": "验收测试环境异常",
    "tool_call_limit": "达到工具调用上限",
    "retry_exhausted": "重试耗尽",
    "workflow_exception": "工作流异常",
    "parallel_branch_failed": "并行分支失败/超时",
}


def _print_run_summary(context: WorkflowContext) -> None:
    print(f"【运行 ID】{context.get('run_id')}")
    requirement = context.get("requirement", {})
    if isinstance(requirement, dict):
        criteria = requirement.get("acceptance_criteria", [])
        if criteria:
            print("【需求验收标准】")
            for criterion in criteria:
                print(f"- {criterion}")
        assumptions = requirement.get("assumptions", [])
        if assumptions:
            print("【实现假设】")
            for assumption in assumptions:
                print(f"- {assumption}")

    tasks = context.get("implementation_tasks", [])
    completed_tasks = context.get("completed_tasks", [])
    if tasks:
        completed_ids = {
            task["id"] for task in completed_tasks if task.get("status") == "passed"
        }
        print("【实施任务进度】")
        for task in tasks:
            status = "通过" if task["id"] in completed_ids else "未完成"
            print(f"- [{status}] {task['id']}: {task['title']}")

    outcome = context.get("workflow_outcome", "unknown")
    print(f"\n【工作流状态】{_OUTCOME_LABELS.get(outcome, outcome)}")

    status = context.get("code_status")
    if status == "ok":
        print(f"【编码】成功，代码已保存到 output/{context.get('output_file')}")
        if context.get("tests_file"):
            print(f"【验收测试】{context.get('test_status')}，文件: output/{context.get('tests_file')}")
    else:
        print(f"【编码】{status or '未启动'}")
        if context.get("test_status"):
            print(f"【验收测试】{context.get('test_status')}")

    if context.issues:
        print("【错误与恢复记录】")
        for issue in context.issues:
            label = _ISSUE_LABELS.get(issue["category"], issue["category"])
            resolution = "已恢复" if issue["resolved"] else "未恢复/需关注"
            attempt = f"，尝试 {issue['attempt']}" if issue["attempt"] is not None else ""
            message = issue["message"]
            if len(message) > 500:
                message = message[:500] + "...（详见执行日志/JSONL）"
            print(f"- [{issue['stage']}] {label}{attempt}（{resolution}）：{message}")
    else:
        print("【错误与恢复记录】无")


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


def ensure_windows_console_utf8():
    """Windows 控制台编码兼容：避免 emoji / 非 GBK 字符导致 UnicodeEncodeError。"""
    if sys.platform.startswith("win"):
        try:
            os.system("chcp 65001 >nul")
        except Exception:
            pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except Exception:
                    pass


def build_workflow(api_key: str, base_url: str, model: str,
                   code_timeout: int, max_retries: int) -> WorkflowEngine:
    """构建并组装 Mini SE 流程：需求 → 设计 → 编码。"""
    llm = LLMClient(api_key=api_key, base_url=base_url, model=model)

    engine = (
        WorkflowEngine()
        .register(RequirementAgent(llm))
        .register(DesignAgent(llm))
        .register(CodingAgent(llm, max_retries=max_retries))
        .register(ConditionalNode(
            predicate=lambda context: context.get("code_status") == "ok",
            if_true=(_WorkflowOutcomeAgent("success"),),
            if_false=(_WorkflowOutcomeAgent("failed"),),
            name="编码结果路由",
        ))
    )
    return engine


def main():
    ensure_windows_console_utf8()
    load_dotenv()
    setup_logging(verbose="--verbose" in sys.argv)

    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model = os.getenv("DASHSCOPE_MODEL", "qwen-plus")
    code_timeout = int(os.getenv("CODE_EXEC_TIMEOUT", "15"))
    max_retries = int(os.getenv("MAX_CODE_RETRIES", "3"))

    if not api_key or api_key == "your_api_key_here":
        print("=" * 50)
        print("错误：未配置 DASHSCOPE_API_KEY")
        print("请复制 .env.example 为 .env 并填入你的 API Key")
        print("获取地址: https://dashscope.console.aliyun.com/")
        print("=" * 50)
        sys.exit(1)

    engine = build_workflow(api_key, base_url, model, code_timeout, max_retries)
    run_store = JsonlRunStore(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "output",
            "workflow_runs.jsonl",
        )
    )
    checkpoint_store = JsonlCheckpointStore(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "output",
            "workflow_checkpoints.jsonl",
        )
    )

    print("=" * 56)
    print("  Mini SE Workflow - 多Agent协作软件开发流程")
    print(f"  模型: {model}")
    print(f"  流程: {engine.describe()}")
    print("      输入 'resume <run_id>' 从最近检查点续跑，输入 'exit' 退出")
    print("=" * 56)

    while True:
        try:
            user_input = input("\n需求描述: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("再见！")
            break

        resume_run_id = None
        start_index = 0
        if user_input.lower() == "resume":
            print("用法：resume <run_id>")
            continue
        if user_input.lower().startswith("resume "):
            resume_run_id = user_input.split(maxsplit=1)[1].strip()
            if not resume_run_id:
                print("用法：resume <run_id>")
                continue
            try:
                context, start_index = checkpoint_store.load_latest(
                    resume_run_id,
                    engine.signature,
                )
            except WorkflowPersistenceError as exc:
                print(f"无法恢复运行：{exc}")
                continue
            if start_index == len(engine.nodes):
                resume_point = "所有节点均已完成"
            else:
                resume_point = f"从节点 {start_index + 1}/{len(engine.nodes)} 继续"
            print(f"恢复 run_id={context.get('run_id')}，{resume_point}")
        else:
            # 每次新需求创建独立运行上下文与产物目录。
            context = WorkflowContext()
            context.set("started_at", datetime.now(timezone.utc).isoformat())
            context.set("user_input", user_input)

        def save_checkpoint(state, completed_index, node):
            checkpoint_store.save(
                state,
                completed_index,
                node.name,
                engine.signature,
            )

        try:
            engine.run(
                context,
                start_index=start_index,
                on_node_complete=save_checkpoint,
            )
        except Exception as exc:
            context.set("workflow_outcome", "error")
            context.record_issue(
                "workflow_exception",
                "工作流",
                f"{type(exc).__name__}: {exc}",
            )
            context.log("Engine", "异常", f"{type(exc).__name__}: {exc}")
            print("\n" + "=" * 56)
            print("  执行结果汇总")
            print("=" * 56)
            _print_run_summary(context)
            try:
                run_store.append(context)
            except WorkflowPersistenceError as persistence_exc:
                raise persistence_exc from exc
            raise
        else:
            run_store.append(context)

        # ---- 汇总输出 ----
        print("\n" + "=" * 56)
        print("  执行结果汇总")
        print("=" * 56)

        requirement = context.get("requirement", {})
        print(f"\n【需求】{requirement.get('title', '(无)') if isinstance(requirement, dict) else requirement}")

        _print_run_summary(context)
        print(f"\n执行日志: {context.dump_logs()}")
        if context.get("code_status") != "ok":
            print("\n建议：根据上方分类处理模型输出、执行环境或验收失败后再重试。")


if __name__ == "__main__":
    main()
