"""可组合的条件分支、循环与并行工作流节点。"""

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from time import monotonic
from typing import Callable, Sequence

from .base import WorkflowNode
from .context import WorkflowCancelled, WorkflowContext


class WorkflowControlError(RuntimeError):
    """控制流无法安全继续时抛出。"""


def _run_nodes(nodes: Sequence[WorkflowNode], context: WorkflowContext) -> None:
    for node in nodes:
        context.check_cancelled()
        node.run(context)
        context.check_cancelled()


class ConditionalNode(WorkflowNode):
    """根据共享状态执行一个分支；未提供的分支视为空操作。"""

    def __init__(
        self,
        predicate: Callable[[WorkflowContext], bool],
        if_true: Sequence[WorkflowNode] = (),
        if_false: Sequence[WorkflowNode] = (),
        name: str = "条件分支",
    ):
        self.name = name
        self.predicate = predicate
        self.if_true = tuple(if_true)
        self.if_false = tuple(if_false)

    def run(self, context: WorkflowContext) -> None:
        selected = bool(self.predicate(context))
        branch = self.if_true if selected else self.if_false
        context.log(self.name, "分支", "条件为真" if selected else "条件为假")
        _run_nodes(branch, context)


class LoopNode(WorkflowNode):
    """当条件为真时重复执行节点序列，达到上限且条件仍为真时显式失败。"""

    def __init__(
        self,
        predicate: Callable[[WorkflowContext], bool],
        body: Sequence[WorkflowNode],
        max_iterations: int,
        name: str = "循环",
    ):
        if max_iterations < 1:
            raise ValueError("max_iterations 必须大于 0")
        self.name = name
        self.predicate = predicate
        self.body = tuple(body)
        self.max_iterations = max_iterations

    def run(self, context: WorkflowContext) -> None:
        iterations = 0
        while self.predicate(context):
            if iterations >= self.max_iterations:
                message = f"循环条件在达到 {self.max_iterations} 次迭代后仍为真"
                context.log(self.name, "错误", message)
                raise WorkflowControlError(message)
            iterations += 1
            context.log(self.name, "迭代", f"第 {iterations}/{self.max_iterations} 次")
            _run_nodes(self.body, context)
        context.log(self.name, "完成", f"共执行 {iterations} 次")


class ParallelNode(WorkflowNode):
    """Run independent branches with deadlines and cooperative cancellation."""

    def __init__(
        self,
        branches: Sequence[Sequence[WorkflowNode]],
        name: str = "并行",
        branch_timeout: float | None = 30.0,
        cancellation_grace: float = 0.5,
    ):
        if not branches:
            raise ValueError("ParallelNode 至少需要一个分支")
        if branch_timeout is not None and branch_timeout <= 0:
            raise ValueError("branch_timeout 必须大于 0 或为 None")
        if cancellation_grace < 0:
            raise ValueError("cancellation_grace 不能小于 0")
        self.name = name
        self.branches = tuple(tuple(branch) for branch in branches)
        self.branch_timeout = branch_timeout
        self.cancellation_grace = cancellation_grace

    def run(self, context: WorkflowContext) -> None:
        try:
            initial_data = deepcopy(context.data)
            initial_logs = deepcopy(context.logs)
            branch_contexts: list[WorkflowContext] = []
            for _ in self.branches:
                branch_context = WorkflowContext(run_id=context.get("run_id"))
                branch_context.data.update(deepcopy(initial_data))
                branch_context.logs.extend(deepcopy(initial_logs))
                branch_contexts.append(branch_context)
        except Exception as exc:
            raise WorkflowControlError(
                "无法复制工作流状态以隔离并行分支"
            ) from exc

        context.log(self.name, "开始", f"并行执行 {len(self.branches)} 个分支")
        executor = ThreadPoolExecutor(max_workers=len(self.branches))
        futures: dict[Future[None], int] = {}
        deadlines: dict[Future[None], float] = {}
        for index, (branch, branch_context) in enumerate(
            zip(self.branches, branch_contexts), start=1
        ):
            future = executor.submit(_run_nodes, branch, branch_context)
            futures[future] = index
            if self.branch_timeout is not None:
                deadlines[future] = monotonic() + self.branch_timeout

        pending = set(futures)
        failures: dict[int, str] = {}
        branch_statuses: dict[int, str] = {}
        while pending and not failures:
            timeout = None
            if deadlines:
                timeout = max(0.0, min(deadlines[future] for future in pending) - monotonic())
            completed, pending = wait(
                pending,
                timeout=timeout,
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                index = futures[future]
                try:
                    future.result()
                    branch_statuses[index] = "成功（状态未合并）"
                except WorkflowCancelled as exc:
                    failures[index] = f"已取消：{exc}"
                    branch_statuses[index] = failures[index]
                except Exception as exc:
                    failures[index] = f"{type(exc).__name__}: {exc}"
                    branch_statuses[index] = failures[index]

            if not completed and pending and deadlines:
                now = monotonic()
                for future in pending:
                    if deadlines[future] <= now:
                        failures[futures[future]] = (
                            f"超过分支超时 {self.branch_timeout:g} 秒"
                        )
                        branch_statuses[futures[future]] = failures[futures[future]]

        if failures:
            for branch_context in branch_contexts:
                branch_context.request_cancellation("其他并行分支失败或超时")
            for future in pending:
                future.cancel()

            grace_deadline = monotonic() + self.cancellation_grace
            remaining = set(pending)
            while remaining and self.cancellation_grace:
                completed, remaining = wait(
                    remaining,
                    timeout=max(0.0, grace_deadline - monotonic()),
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    index = futures[future]
                    if future.cancelled():
                        failures.setdefault(index, "尚未启动，已取消")
                        branch_statuses[index] = failures[index]
                        continue
                    try:
                        future.result()
                        failures.setdefault(index, "收到取消请求后已停止")
                        branch_statuses[index] = failures[index]
                    except WorkflowCancelled as exc:
                        failures.setdefault(index, f"已协作取消：{exc}")
                        branch_statuses[index] = failures[index]
                    except Exception as exc:
                        failures.setdefault(index, f"{type(exc).__name__}: {exc}")
                        branch_statuses[index] = failures[index]

            for future in remaining:
                failures.setdefault(
                    futures[future],
                    "取消请求已发送，但节点未在协作取消宽限期内退出",
                )
                branch_statuses[futures[future]] = failures[futures[future]]
            executor.shutdown(wait=False, cancel_futures=True)
            failure_summary = "; ".join(
                f"分支 {index}: {branch_statuses.get(index, '未完成')}"
                for index in range(1, len(self.branches) + 1)
            )
            message = f"并行执行失败，未合并任何分支状态。{failure_summary}"
            context.record_issue(
                "parallel_branch_failed",
                self.name,
                failure_summary,
            )
            context.log(self.name, "失败", message)
            raise WorkflowControlError(message)

        executor.shutdown(wait=True)

        missing = object()
        changes: dict[str, object] = {}
        for branch_context in branch_contexts:
            branch_changes = {}
            for key in initial_data.keys() | branch_context.data.keys():
                before = initial_data.get(key, missing)
                after = branch_context.data.get(key, missing)
                if before is missing or after is missing:
                    if before is not after:
                        branch_changes[key] = after
                elif before != after:
                    branch_changes[key] = after

            for key, value in branch_changes.items():
                if key in changes:
                    existing = changes[key]
                    equal = existing is value
                    if not equal and existing is not missing and value is not missing:
                        equal = existing == value
                    if not equal:
                        message = f"并行分支对共享状态键 {key!r} 写入冲突"
                        context.log(self.name, "冲突", message)
                        raise WorkflowControlError(message)
                changes[key] = value

        for key, value in changes.items():
            if value is missing:
                context.data.pop(key, None)
            else:
                context.set(key, value)

        for branch_context in branch_contexts:
            context.logs.extend(branch_context.logs[len(initial_logs):])
        context.log(self.name, "完成", "并行分支执行并合并成功")
