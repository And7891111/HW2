"""Concurrent-safe JSONL persistence for run summaries and node checkpoints."""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Generator

from .context import WorkflowContext


class PersistenceError(RuntimeError):
    """Raised when workflow state cannot be safely saved or restored."""


WorkflowPersistenceError = PersistenceError

_LOCAL_LOCKS: dict[Path, Lock] = {}
_LOCAL_LOCKS_GUARD = Lock()


def _local_lock(path: Path) -> Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(path, Lock())


@contextmanager
def _file_lock(path: Path) -> Generator[None, None, None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _local_lock(lock_path):
        with lock_path.open("a+b") as lock_file:
            try:
                if os.name == "nt":
                    import msvcrt

                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise PersistenceError(f"无法锁定持久化文件 {path}: {exc}") from exc
            try:
                yield
            finally:
                try:
                    if os.name == "nt":
                        import msvcrt

                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded = json.dumps(record, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"运行状态无法序列化：{exc}") from exc
    with _file_lock(path):
        try:
            with path.open("a", encoding="utf-8", newline="") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
        except OSError as exc:
            raise PersistenceError(f"无法写入持久化文件 {path}: {exc}") from exc


class JsonlRunStore:
    """Append final run records without interleaving concurrent writers."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, context: WorkflowContext) -> dict[str, Any]:
        record = {
            "run_id": context.get("run_id"),
            "started_at": context.get("started_at"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "outcome": context.get("workflow_outcome"),
            "state": context.data,
            "logs": context.logs,
        }
        _append_record(self.path, record)
        return record


class JsonlCheckpointStore:
    """Append and restore the latest completed-node snapshot for each run."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(
        self,
        context: WorkflowContext,
        completed_index: int,
        completed_node: str,
        workflow_signature: str,
    ) -> None:
        _append_record(
            self.path,
            {
                "run_id": context.get("run_id"),
                "completed_index": completed_index,
                "completed_node": completed_node,
                "workflow_signature": workflow_signature,
                "data": context.data,
                "logs": context.logs,
            },
        )

    def load_latest(
        self,
        run_id: str,
        expected_signature: str,
    ) -> tuple[WorkflowContext, int]:
        if not self.path.exists():
            raise PersistenceError(f"找不到检查点文件：{self.path}")

        latest: dict[str, Any] | None = None
        with _file_lock(self.path):
            try:
                with self.path.open("r", encoding="utf-8") as file:
                    for line_number, line in enumerate(file, start=1):
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise PersistenceError(
                                f"检查点文件第 {line_number} 行不是有效 JSON"
                            ) from exc
                        if record.get("run_id") == run_id:
                            latest = record
            except OSError as exc:
                raise PersistenceError(
                    f"无法读取检查点文件 {self.path}: {exc}"
                ) from exc

        if latest is None:
            raise PersistenceError(f"找不到 run_id={run_id} 的检查点")
        if latest.get("workflow_signature") != expected_signature:
            raise PersistenceError(
                "当前 Workflow 与检查点中的 Workflow 不匹配，拒绝恢复"
            )
        try:
            data = latest["data"]
            logs = latest["logs"]
            if not isinstance(data, dict) or not isinstance(logs, list):
                raise PersistenceError("检查点状态或日志类型无效")
            if any(
                not isinstance(entry, list)
                or len(entry) != 3
                or not all(isinstance(item, str) for item in entry)
                for entry in logs
            ):
                raise PersistenceError("检查点日志项结构无效")
            context = WorkflowContext.from_snapshot(
                run_id=run_id,
                data=data,
                logs=logs,
            )
            completed_index = int(latest["completed_index"])
        except PersistenceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(f"检查点结构无效：{exc}") from exc
        if completed_index < 1:
            raise PersistenceError("检查点中的已完成节点索引无效")
        return context, completed_index
