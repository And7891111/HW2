"""共享状态对象：Workflow 中所有 Agent 共同读写的状态容器。

这是 Agent 间"状态共享"与"消息传递"的核心：
- 前一个 Agent 把产出写入 context 的某个字段
- 后一个 Agent 从 context 读取该字段继续工作
"""

from threading import Event
from typing import Any
from uuid import UUID, uuid4


class WorkflowCancelled(RuntimeError):
    """Raised when a workflow node observes a cooperative cancellation request."""


class WorkflowContext:
    """一个简单的共享状态字典，支持点号访问与日志记录。"""

    def __init__(self, run_id: str | None = None):
        selected_run_id = str(UUID(run_id)) if run_id else str(uuid4())
        self._data: dict[str, Any] = {"run_id": selected_run_id}
        self._data["issues"] = []
        self._cancel_event = Event()
        self._cancel_reason: str | None = None
        # 执行日志：[(节点名, 动作, 消息)]
        self.logs: list[tuple[str, str, str]] = []

    @classmethod
    def from_snapshot(
        cls,
        run_id: str,
        data: dict[str, Any],
        logs: list[tuple[str, str, str]],
    ) -> "WorkflowContext":
        context = cls(run_id=run_id)
        context._data.update(data)
        context._data["run_id"] = run_id
        context._data.setdefault("issues", [])
        context.logs = [tuple(entry) for entry in logs]
        return context

    # ---- 状态读写（Agent 间传递消息的核心） ----
    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._data

    # ---- 日志记录（推荐加分项：执行日志与状态追踪） ----
    def log(self, node: str, action: str, message: str) -> None:
        self.logs.append((node, action, message))

    def request_cancellation(self, reason: str) -> None:
        self._cancel_reason = reason
        self._cancel_event.set()

    @property
    def cancellation_requested(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def cancellation_reason(self) -> str | None:
        return self._cancel_reason

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise WorkflowCancelled(self._cancel_reason or "工作流取消请求")

    def record_issue(
        self,
        category: str,
        stage: str,
        message: str,
        *,
        attempt: int | None = None,
        recoverable: bool = False,
    ) -> dict[str, Any]:
        """Record a classified run issue for summaries and persisted snapshots."""
        issue = {
            "category": category,
            "stage": stage,
            "message": message,
            "attempt": attempt,
            "recoverable": recoverable,
            "resolved": False,
        }
        self._data["issues"].append(issue)
        return issue

    def resolve_issues(
        self,
        stage: str,
        categories: set[str] | None = None,
    ) -> None:
        """Mark earlier recoverable issues as resolved after a later success."""
        for issue in self._data["issues"]:
            if (
                issue["stage"] == stage
                and issue["recoverable"]
                and not issue["resolved"]
                and (categories is None or issue["category"] in categories)
            ):
                issue["resolved"] = True

    @property
    def issues(self) -> list[dict[str, Any]]:
        return self._data["issues"]

    def dump_logs(self) -> str:
        lines = ["\n=== Workflow 执行日志 ==="]
        for node, action, message in self.logs:
            lines.append(f"  [{node}] {action}: {message}")
        return "\n".join(lines)

    @property
    def data(self) -> dict[str, Any]:
        return self._data
