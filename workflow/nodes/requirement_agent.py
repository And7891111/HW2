"""需求 Agent：把用户原始输入整理成清晰、结构化、可执行的需求说明。"""

import logging
from ..base import Agent
from ..context import WorkflowContext
from .structured_output import (
    RequirementOutput,
    StructuredOutputError,
    parse_json_object,
    validate_requirement,
)

logger = logging.getLogger(__name__)
MAX_OUTPUT_ATTEMPTS = 2


class RequirementAgent(Agent):
    """需求Agent：接收用户原始需求，产出结构化需求文档。

    状态约定（本节点写入 context）：
    - context.requirement   : 经过 schema 校验的需求说明
    - context.requirement_degraded : 校验/请求失败后是否使用降级结果
    - context.requirement_error    : 最终失败原因（若有）
    """

    def __init__(self, llm_client, name: str = "需求Agent"):
        super().__init__(name)
        self.llm = llm_client

    def run(self, context: WorkflowContext) -> None:
        user_input = context.get("user_input", "")
        if not user_input:
            context.log(self.name, "错误", "未收到用户输入")
            context.record_issue("missing_input", "需求", "未收到用户输入")
            context.set("requirement", "")
            return

        context.log(self.name, "开始", f"收到原始需求: {user_input[:50]}...")

        prompt = f"""        你是一名谨慎的软件需求分析师。请把用户的原始需求整理成清晰、结构化的需求说明。

原始需求：
{user_input}

请严格按照以下 JSON 格式输出（不要输出其他内容）：
{{
  "title": "功能标题",
  "description": "一句话描述功能",
  "inputs": ["输入项1", "输入项2"],
  "expected_output": "期望的输出结果",
  "constraints": ["技术、性能、安全和兼容性约束"],
  "acceptance_criteria": ["可以通过输入/输出验证的验收标准，至少一项"],
  "assumptions": ["需求没有说明但实现必须暂定的假设；无假设时返回空列表"]
}}

必须区分“用户明确提出的需求”和“你推测的设计选择”：
- 只把原文明确提到的输入字段、功能、校验规则和输出行为写入 inputs、constraints、acceptance_criteria。
- 不要自行添加姓名、年龄、邮箱等字段，也不要自行添加格式/范围校验、重试次数、异常类型或退出行为；只有原文明确要求时才能列为需求。
- 用户说“个人信息”等范围不具体时，不要猜一长串字段；将最小必要的暂定方案写入 assumptions，后续实现提示也必须视为建议而非验收要求。
- 验收标准应是用户原文的可观察行为，优先覆盖核心成功路径；只有用户明确提到边界或无效输入时才增加对应标准。不要为了增加标准而编造规则。
- 不要把未明确的业务规则伪装成确定需求；将暂定取舍写入 assumptions。
"""

        errors: list[str] = []
        requirement: RequirementOutput | None = None
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(1, MAX_OUTPUT_ATTEMPTS + 1):
            try:
                raw = self.llm.chat(messages)
                requirement = validate_requirement(
                    parse_json_object(raw.get("content", ""))
                )
                break
            except StructuredOutputError as exc:
                errors.append(str(exc))
                context.record_issue(
                    "structured_output_invalid",
                    "需求",
                    str(exc),
                    attempt=attempt,
                    recoverable=True,
                )
                context.log(
                    self.name,
                    "校验失败",
                    f"第 {attempt}/{MAX_OUTPUT_ATTEMPTS} 次输出不符合需求 schema: {exc}",
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
                logger.exception("需求Agent模型调用失败")
                errors.append(f"{type(exc).__name__}: {exc}")
                context.record_issue(
                    "llm_call_failed",
                    "需求",
                    f"{type(exc).__name__}: {exc}",
                    attempt=attempt,
                )
                context.log(self.name, "调用失败", f"第 {attempt} 次生成需求异常: {exc}")
                break

        if requirement is None:
            error = "；".join(errors) or "未知结构化输出错误"
            requirement = self._fallback(user_input)
            context.record_issue("stage_degraded", "需求", error)
            context.set("requirement_degraded", True)
            context.set("requirement_error", error)
            context.log(self.name, "降级", f"使用原始用户需求作为降级结果；原因: {error}")
        else:
            context.resolve_issues("需求")
            context.set("requirement_degraded", False)
            context.set("requirement_error", "")

        context.set("requirement", requirement)
        context.log(
            self.name,
            "完成",
            f"已生成需求文档，标题: {requirement['title']}",
        )

    @staticmethod
    def _fallback(user_input: str) -> RequirementOutput:
        """Build an explicit, schema-valid fallback from the original request."""
        return {
            "title": user_input[:80],
            "description": user_input,
            "inputs": [],
            "expected_output": "按原始需求实现并验证",
            "constraints": ["需求结构化失败；请核对原始需求并补充缺失信息"],
            "acceptance_criteria": ["按原始需求实现核心行为并通过验收测试"],
            "assumptions": ["需求结构化失败；编码前应复核原始需求"],
        }
