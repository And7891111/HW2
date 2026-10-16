"""Schemas and validation helpers for structured Agent responses."""

import json
import re
from typing import TypedDict


class RequirementOutput(TypedDict):
    title: str
    description: str
    inputs: list[str]
    expected_output: str
    constraints: list[str]
    acceptance_criteria: list[str]
    assumptions: list[str]


class DesignOutput(TypedDict):
    approach: str
    structure: str
    function_signature: str
    test_plan: str
    impl_notes: list[str]
    tasks: list["ImplementationTask"]


class ImplementationTask(TypedDict):
    id: str
    title: str
    description: str
    depends_on: list[str]
    acceptance_criteria: list[str]


class StructuredOutputError(ValueError):
    """Raised when an Agent response does not match its required JSON schema."""


def parse_json_object(content: str) -> dict:
    """Parse a JSON object, allowing a single Markdown JSON code fence."""
    if not isinstance(content, str) or not content.strip():
        raise StructuredOutputError("模型回复为空")

    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}"
        ) from exc

    if not isinstance(value, dict):
        raise StructuredOutputError("JSON 顶层必须是对象")
    return value


def validate_requirement(value: dict) -> RequirementOutput:
    required = {
        "title",
        "description",
        "inputs",
        "expected_output",
        "constraints",
        "acceptance_criteria",
        "assumptions",
    }
    _validate_keys(value, required, "需求")
    _validate_nonempty_string(value, "title", "需求")
    _validate_nonempty_string(value, "description", "需求")
    _validate_string_list(value, "inputs", "需求")
    _validate_nonempty_string(value, "expected_output", "需求")
    _validate_string_list(value, "constraints", "需求")
    _validate_string_list(value, "acceptance_criteria", "需求", require_items=True)
    _validate_string_list(value, "assumptions", "需求")
    return {
        "title": value["title"].strip(),
        "description": value["description"].strip(),
        "inputs": [item.strip() for item in value["inputs"]],
        "expected_output": value["expected_output"].strip(),
        "constraints": [item.strip() for item in value["constraints"]],
        "acceptance_criteria": [
            item.strip() for item in value["acceptance_criteria"]
        ],
        "assumptions": [item.strip() for item in value["assumptions"]],
    }


def validate_design(
    value: dict,
    required_criteria: list[str] | None = None,
) -> DesignOutput:
    required = {
        "approach",
        "structure",
        "function_signature",
        "test_plan",
        "impl_notes",
        "tasks",
    }
    _validate_keys(value, required, "设计")
    for field in ("approach", "structure", "function_signature", "test_plan"):
        _validate_nonempty_string(value, field, "设计")
    _validate_string_list(value, "impl_notes", "设计")
    tasks = _validate_tasks(value["tasks"])
    task_criteria = {
        criterion
        for task in tasks
        for criterion in task["acceptance_criteria"]
    }
    uncovered = sorted(set(required_criteria or []) - task_criteria)
    if uncovered:
        raise StructuredOutputError(
            "任务计划未逐字覆盖以下需求验收标准: " + "；".join(uncovered)
        )
    return {
        "approach": value["approach"].strip(),
        "structure": value["structure"].strip(),
        "function_signature": value["function_signature"].strip(),
        "test_plan": value["test_plan"].strip(),
        "impl_notes": [item.strip() for item in value["impl_notes"]],
        "tasks": tasks,
    }


def _validate_keys(value: dict, required: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - required)
    problems = []
    if missing:
        problems.append(f"缺少字段: {', '.join(missing)}")
    if unexpected:
        problems.append(f"包含未知字段: {', '.join(unexpected)}")
    if problems:
        raise StructuredOutputError(f"{label}结构不符合 schema（{'；'.join(problems)}）")


def _validate_nonempty_string(value: dict, field: str, label: str) -> None:
    content = value[field]
    if not isinstance(content, str) or not content.strip():
        raise StructuredOutputError(
            f"{label}字段 {field!r} 必须是非空字符串"
        )


def _validate_string_list(
    value: dict,
    field: str,
    label: str,
    *,
    require_items: bool = False,
) -> None:
    items = value[field]
    if not isinstance(items, list) or any(
        not isinstance(item, str) or not item.strip() for item in items
    ):
        raise StructuredOutputError(
            f"{label}字段 {field!r} 必须是字符串列表，且列表项不可为空"
        )
    if require_items and not items:
        raise StructuredOutputError(f"{label}字段 {field!r} 至少需要一项")


def _validate_tasks(value: object) -> list[ImplementationTask]:
    if not isinstance(value, list) or not value:
        raise StructuredOutputError("设计字段 'tasks' 必须是非空任务列表")
    if len(value) > 8:
        raise StructuredOutputError("设计任务数量不能超过 8 项")

    required = {"id", "title", "description", "depends_on", "acceptance_criteria"}
    normalized: list[ImplementationTask] = []
    task_ids: set[str] = set()
    for index, task in enumerate(value):
        if not isinstance(task, dict):
            raise StructuredOutputError(f"tasks[{index}] 必须是 JSON 对象")
        _validate_keys(task, required, f"任务 tasks[{index}]")
        for field in ("id", "title", "description"):
            _validate_nonempty_string(task, field, f"任务 tasks[{index}]")
        _validate_string_list(task, "depends_on", f"任务 tasks[{index}]")
        _validate_string_list(
            task,
            "acceptance_criteria",
            f"任务 tasks[{index}]",
            require_items=True,
        )
        task_id = task["id"].strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", task_id):
            raise StructuredOutputError(
                f"任务 ID {task_id!r} 只能包含字母、数字、下划线或连字符"
            )
        if task_id in task_ids:
            raise StructuredOutputError(f"任务 ID 重复: {task_id}")
        task_ids.add(task_id)
        normalized.append({
            "id": task_id,
            "title": task["title"].strip(),
            "description": task["description"].strip(),
            "depends_on": [item.strip() for item in task["depends_on"]],
            "acceptance_criteria": [
                item.strip() for item in task["acceptance_criteria"]
            ],
        })

    for task in normalized:
        if len(set(task["depends_on"])) != len(task["depends_on"]):
            raise StructuredOutputError(f"任务 {task['id']} 的依赖项重复")
        missing = sorted(set(task["depends_on"]) - task_ids)
        if missing:
            raise StructuredOutputError(
                f"任务 {task['id']} 依赖不存在的任务: {', '.join(missing)}"
            )
        if task["id"] in task["depends_on"]:
            raise StructuredOutputError(f"任务 {task['id']} 不能依赖自身")

    ordered: list[ImplementationTask] = []
    completed: set[str] = set()
    while len(ordered) < len(normalized):
        ready = [
            task for task in normalized
            if task["id"] not in completed
            and set(task["depends_on"]) <= completed
        ]
        if not ready:
            raise StructuredOutputError("任务依赖关系存在循环")
        for task in ready:
            ordered.append(task)
            completed.add(task["id"])
    return ordered
