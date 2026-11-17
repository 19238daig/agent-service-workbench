"""
合规审查Agent — 金融/电商场景合规检查
负责对所有Agent的回复进行合规审查，包括：
- 敏感词检测
- PII（个人身份信息）保护
- 金融合规用语检查
- 越权承诺检测
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from smart_cs.agents.context import append_step, latest_user_turn, record_model
from smart_cs.tracing.otel_config import trace_agent_call
from smart_cs.observability.models import invoke_model
from smart_cs.agents.json_output import parse_json_object


@dataclass
class ComplianceResult:
    """合规审查结果"""
    passed: bool
    risk_level: str  # low, medium, high, critical
    violations: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    sanitized_content: str = ""


SENSITIVE_PATTERNS = {
    "phone": r"1[3-9]\d{9}",
    "id_card": r"\d{17}[\dXx]",
    "bank_card": r"\d{16,19}",
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
}

FORBIDDEN_TERMS = [
    "保证收益", "稳赚不赔", "零风险", "保本保息",
    "最高收益", "预期收益率", "承诺回报",
    "内部消息", "内幕", "暗箱操作",
]

COMPLIANCE_SYSTEM_PROMPT = """你是虚构电商售后客服的合规审查Agent。检查内容是否与所给演示规则及真实工具结果一致，不将申请登记误称为实际退款、审批通过、冻结或赔付。可以描述申请流程和实际已创建的工单。金融风险仅作安全提示，不办理金融业务。

审查维度：
1. 是否包含违规金融用语（如"保证收益"、"零风险"等）
2. 是否泄露用户PII信息（手机号、身份证号、银行卡号）
3. 是否存在越权承诺（如擅自承诺退款/赔偿金额）
4. 是否符合金融监管要求（风险提示、免责声明）
5. 是否包含歧视性、侮辱性内容

请以JSON格式返回审查结果：
{
    "passed": true/false,
    "risk_level": "low|medium|high|critical",
    "violations": ["违规项描述"],
    "suggestions": ["修改建议"]
}
"""


class ComplianceCheckerAgent:
    """合规审查Agent"""

    def __init__(self, llm: ChatOpenAI, tool_service=None,execution_service=None):
        self.llm = llm
        self.tool_service=tool_service
        self.execution_service=execution_service

    async def review_execution(self,state):
        if not state.get('action_execution'):return {}
        from smart_cs.services.access import principal_context
        from smart_cs.services.execution import execution_text
        from smart_cs.services.compliance import ComplianceReviewService,release_allowed
        result=await self.execution_service.get(principal_context.get(),state['action_execution']['action_id'])
        updated={**state,'action_execution':result,'knowledge_evidence':{},'sub_results':{'execution':execution_text(result)}}
        review=await ComplianceReviewService(self.llm,self.tool_service,execution_service=self.execution_service).review_content(execution_text(result),updated)
        updated['content_review']=review.model_dump(mode='json')
        updated['compliance_passed']=release_allowed(updated)
        return {**updated,'agent_steps':append_step(state,'execution_review'),
            'agent_models':record_model(state,'compliance_checker',self.llm)}

    @trace_agent_call('compliance_action_review')
    async def review_action(self,state):
        from smart_cs.services.compliance import ComplianceReviewService,ACTION_BLOCK
        from smart_cs.services.access import principal_context
        raw=state.get('action_plan') or {}
        if not raw:return {**state,'action_review':{},'agent_steps':append_step(state,'action_review')}
        review=await ComplianceReviewService(self.llm,self.tool_service).review_action(principal_context.get(),raw)
        approved=review.decision in {'allow','require_approval'}
        results=dict(state.get('sub_results',{}))
        if approved and not raw.get('read_only'):
            from smart_cs.schemas.actions import PlanValidation
            from smart_cs.services.actions import format_plan
            results['ticket_handler']=format_plan(PlanValidation.model_validate(raw),approval=review.required_approval)
        if not approved:
            fields=raw.get('missing_fields',[]) if review.decision=='clarify' else []
            # Only server-defined slot names are reflected, never rejected text.
            fields=[f for f in fields if f in {'order_id','requested_amount','summary','details'}]
            results['ticket_handler']=('请补充或核实：'+', '.join(fields)+'。本轮不会创建工单或退款。') if fields else ACTION_BLOCK
        return {**state,'action_plan':raw if approved else {},'action_review':review.model_dump(mode='json'),
            'sub_results':results,'current_agent':'compliance_checker','agent_steps':append_step(state,'action_review'),
            'agent_models':record_model(state,'compliance_checker',self.llm)}

    def _rule_based_check(self, content: str) -> list[str]:
        """基于规则的快速检查（不依赖LLM，低延迟）"""
        violations = []

        for term in FORBIDDEN_TERMS:
            if term in content:
                violations.append(f"包含违规金融用语: '{term}'")

        for pii_type, pattern in SENSITIVE_PATTERNS.items():
            if re.search(pattern, content):
                label = {
                    "phone": "手机号", "id_card": "身份证号",
                    "bank_card": "银行卡号", "email": "邮箱地址",
                }.get(pii_type, pii_type)
                violations.append(f"检测到PII信息泄露: {label}")

        return violations

    def _mask_pii(self, content: str) -> str:
        """对PII信息进行脱敏处理"""
        masked = content
        for pii_type, pattern in SENSITIVE_PATTERNS.items():
            def _mask_match(match):
                text = match.group()
                if len(text) <= 4:
                    return "****"
                return text[:3] + "*" * (len(text) - 6) + text[-3:]
            masked = re.sub(pattern, _mask_match, masked)
        return masked

    @trace_agent_call("compliance_rule_check")
    async def rule_check(self, content: str) -> ComplianceResult:
        """规则引擎快速检查"""
        violations = self._rule_based_check(content)
        sanitized = self._mask_pii(content)

        if not violations:
            return ComplianceResult(
                passed=True,
                risk_level="low",
                sanitized_content=sanitized,
            )

        has_pii = any("PII" in v for v in violations)
        has_forbidden = any("违规金融用语" in v for v in violations)

        if has_pii and has_forbidden:
            risk_level = "critical"
        elif has_pii or has_forbidden:
            risk_level = "high"
        else:
            risk_level = "medium"

        return ComplianceResult(
            passed=False,
            risk_level=risk_level,
            violations=violations,
            sanitized_content=sanitized,
        )

    @trace_agent_call("compliance_llm_check")
    async def llm_check(self, content: str) -> ComplianceResult:
        """LLM深度合规审查（处理规则引擎无法覆盖的场景）"""
        messages = [
            SystemMessage(content=COMPLIANCE_SYSTEM_PROMPT),
            HumanMessage(content=f"请审查以下客服回复内容的合规性：\n\n{content}"),
        ]

        response = await invoke_model(self.llm, messages)

        try:
            result = parse_json_object(response.content)
        except ValueError:
            result = {}
        level = result.get("risk_level")
        if not isinstance(result.get("passed"), bool) or not isinstance(level, str) or level not in {"low", "medium", "high", "critical"}:
            return ComplianceResult(passed=False, risk_level="medium", violations=["合规审查未返回有效结论"], sanitized_content=self._mask_pii(content))
        violations = result.get("violations", [])
        suggestions = result.get("suggestions", [])
        return ComplianceResult(
            passed=result["passed"] and result["risk_level"] in {"low", "medium"},
            risk_level=result["risk_level"],
            violations=[v for v in violations if isinstance(v, str)] if isinstance(violations, list) else [],
            suggestions=[v for v in suggestions if isinstance(v, str)] if isinstance(suggestions, list) else [],
            sanitized_content=self._mask_pii(content),
        )

    @trace_agent_call("compliance_full_check")
    async def full_check(self, content: str) -> ComplianceResult:
        """
        两阶段合规审查：
        1. 规则引擎快速检查（毫秒级）
        2. 若规则通过，再进行LLM深度审查
        """
        rule_result = await self.rule_check(content)

        if not rule_result.passed and rule_result.risk_level in ("high", "critical"):
            return rule_result

        llm_result = await self.llm_check(content)

        all_violations = rule_result.violations + llm_result.violations
        final_passed = rule_result.passed and llm_result.passed

        risk_priority = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        final_risk = max(
            rule_result.risk_level, llm_result.risk_level,
            key=lambda r: risk_priority.get(r, 0),
        )

        return ComplianceResult(
            passed=final_passed,
            risk_level=final_risk,
            violations=all_violations,
            suggestions=llm_result.suggestions,
            sanitized_content=rule_result.sanitized_content,
        )

    @trace_agent_call("compliance_request")
    async def handle_request(self, state: dict[str, Any]) -> str:
        query, history = latest_user_turn(state.get("messages", []))
        response = await invoke_model(self.llm, [
            SystemMessage(content=(
                "你负责账户异常、欺诈和资金安全咨询。针对最新问题给出简洁、通用的核实与求助建议。"
                "不要索取或复述密码、验证码、身份证、银行卡等隐私；不要建议向可疑账户转账。"
                "不要声称已冻结账户、转交人工、建单或完成任何外部操作；需要介入时建议通过平台官方渠道联系人工。"
                "不要编造联系电话、网站或处理结果。只返回给用户的答复。"
            )),
            *history,
            HumanMessage(content=query),
        ])
        return response.content.strip()

    @trace_agent_call("compliance_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.tool_service is not None:
            return await self.process_strict(state)
        sub_results = dict(state.get("sub_results", {}))
        content = "\n".join(value for value in sub_results.values() if isinstance(value, str) and value.strip())
        if not content.strip() and state.get("intent") == "compliance_checker":
            content = await self.handle_request(state)
            sub_results["compliance_reply"] = content
        if content.strip():
            result = await self.full_check(content)
        else:
            result = ComplianceResult(passed=False, risk_level="medium", violations=["没有可审查的回复内容"])
        if not result.passed:
            sub_results = {key: self._mask_pii(value) if isinstance(value, str) else value for key, value in sub_results.items()}
        sub_results["compliance"] = {
            "passed": result.passed, "risk_level": result.risk_level, "violations": result.violations,
        }
        return {
            **state, "compliance_passed": result.passed, "sub_results": sub_results,
            "current_agent": "compliance_checker", "agent_steps": append_step(state, "compliance_check"),
            "agent_models": record_model(state, "compliance_checker", self.llm),
        }

    async def process_strict(self,state):
        from smart_cs.services.compliance import ComplianceReviewService,candidate_text,release_allowed
        results=dict(state.get('sub_results',{}))
        if not candidate_text(state) and state.get('intent')=='compliance_checker':
            try:results['compliance_reply']=await self.handle_request(state)
            except Exception:results['compliance_reply']=''
        state={**state,'sub_results':results}
        review=await ComplianceReviewService(self.llm,self.tool_service).review_content(candidate_text(state),state)
        state={**state,'content_review':review.model_dump(mode='json')}
        passed=release_allowed(state)
        results['compliance']={'passed':passed,'risk_level':review.risk_level,'violations':list(review.reason_codes) if not passed else []}
        return {**state,'sub_results':results,'compliance_passed':passed,'current_agent':'compliance_checker',
            'agent_steps':append_step(state,'compliance_check'),'agent_models':record_model(state,'compliance_checker',self.llm)}
