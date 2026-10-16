"""设计 Agent：基于需求文档产出软件设计方案（结构、接口、模块划分）。"""

import json
import logging
from ..base import Agent
from ..context import WorkflowContext
from .structured_output import (
    DesignOutput,
    StructuredOutputError,
    parse_json_object,
    validate_design,
)

logger = logging.getLogger(__name__)
MAX_OUTPUT_ATTEMPTS = 2


class DesignAgent(Agent):
    """设计Agent：读取需求，产出技术设计与实现要点。

    状态约定（读取/写入 context）：
    - 读取 context.requirement
    - 写入 context.design
    - 写入 context.design_degraded 与 context.design_error，标记降级状态和原因
    """

    def __init__(self, llm_client, name: str = "设计Agent"):
        super().__init__(name)
        self.llm = llm_client

    def run(self, context: WorkflowContext) -> None:
        requirement = context.get("requirement", {})
        if not requirement:
            context.log(self.name, "错误", "没有需求文档，无法设计")
            context.record_issue(
                "missing_requirement",
                "设计",
                "没有可用需求文档，设计阶段无法开始",
            )
            context.set("design", "")
            return

        title = requirement.get("title", "(无标题)") if isinstance(requirement, dict) else "(无标题)"
        context.log(self.name, "开始", f"基于需求设计: {title}")

        req_text = json.dumps(requirement, ensure_ascii=False, indent=2)
        prompt = f"""        你是一名软件架构设计师。请根据用户原始需求和结构化需求说明，产出简洁清晰的 Python 设计方案。不得把 assumptions 中未获用户确认的内容转成必做功能或验收条件。

        用户原始需求：
        {context.get("user_input", "")}

        需求文档：
        {req_text}

请严格按照以下 JSON 格式输出（不要输出其他内容）：
{{
  "approach": "整体技术方案（几句话）",
  "structure": "代码结构/模块划分",
  "function_signature": "核心函数签名及说明",
  "test_plan": "如何测试这个功能（用代码自测，不依赖人工输入）",
  "impl_notes": ["实现注意点1", "实现注意点2"],
  "tasks": [
    {{
      "id": "稳定的短英文标识",
      "title": "任务标题",
      "description": "实现边界明确的任务描述",
      "depends_on": [],
      "acceptance_criteria": ["可独立验证的任务验收标准"]
    }}
  ]
}}

任务拆分规则：
- 简单的单程序/单屏幕/单一用户流程默认只生成 1 个任务；不要为了拆分而拆分。
- 只有原始需求明确包含多个独立子系统、多个模块/服务或明显不同的交付物时，才拆成多个任务；最多 8 项。
- 不得仅因字段数量、校验函数或测试数量而把一个小程序拆成许多任务。
- 任务验收标准必须来自用户明确提出的要求；每项需求标准至少被一个任务覆盖。
- 依赖只能引用任务 id，且依赖图必须无环；每项任务保持有意义、可独立验证。
"""

        errors: list[str] = []
        design: DesignOutput | None = None
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(1, MAX_OUTPUT_ATTEMPTS + 1):
            try:
                raw = self.llm.chat(messages)
                design = validate_design(
                    parse_json_object(raw.get("content", "")),
                    required_criteria=(
                        requirement.get("acceptance_criteria", [])
                        if isinstance(requirement, dict)
                        else []
                    ),
                )
                break
            except StructuredOutputError as exc:
                errors.append(str(exc))
                context.record_issue(
                    "structured_output_invalid",
                    "设计",
                    str(exc),
                    attempt=attempt,
                    recoverable=True,
                )
                context.log(
                    self.name,
                    "校验失败",
                    f"第 {attempt}/{MAX_OUTPUT_ATTEMPTS} 次输出不符合设计 schema: {exc}",
                )
                if attempt < MAX_OUTPUT_ATTEMPTS:
                    messages.append({
                        "role": "assistant",
                        "content": raw.get("content", ""),
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            "上一条回复未通过 JSON schema 校验。请根据以下错误修正，"
                            "只返回包含全部必需字段的 JSON 对象，不要添加其他字段或说明：\n"
                            f"{exc}"
                        ),
                    })
            except Exception as exc:
                logger.exception("设计Agent模型调用失败")
                errors.append(f"{type(exc).__name__}: {exc}")
                context.record_issue(
                    "llm_call_failed",
                    "设计",
                    f"{type(exc).__name__}: {exc}",
                    attempt=attempt,
                )
                context.log(self.name, "调用失败", f"第 {attempt} 次生成设计异常: {exc}")
                break

        if design is None:
            error = "；".join(errors) or "未知结构化输出错误"
            design = self._fallback()
            context.record_issue("stage_degraded", "设计", error)
            context.set("design_degraded", True)
            context.set("design_error", error)
            context.log(self.name, "降级", f"使用基础设计作为降级结果；原因: {error}")
        else:
            context.resolve_issues("设计")
            context.set("design_degraded", False)
            context.set("design_error", "")

        context.set("design", design)
        context.log(
            self.name,
            "完成",
            f"设计方案产出，核心函数: {design['function_signature']}",
        )

    @staticmethod
    def _fallback() -> DesignOutput:
        return {
            "approach": "按已校验的需求直接实现最小可运行方案",
            "structure": "单模块实现",
            "function_signature": "def main() -> None",
            "test_plan": "使用标准库 unittest 覆盖正常输入、边界和无效输入",
            "impl_notes": ["设计生成或校验失败；实现前需复核需求与接口"],
            "tasks": [{
                "id": "implement-core",
                "title": "实现完整功能",
                "description": "按原始需求实现最小可运行方案。",
                "depends_on": [],
                "acceptance_criteria": ["核心需求可通过验收测试验证"],
            }],
        }
