"""Classify the current user turn and extract routing metadata."""
from __future__ import annotations

from dataclasses import dataclass,field
from enum import Enum
import math
import json
import logging
from smart_cs.schemas.tasks import TaskProposal
import re
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from smart_cs.agents.context import latest_user_turn, record_model
from smart_cs.agents.json_output import parse_json_object
from smart_cs.tracing.otel_config import trace_agent_call
from smart_cs.observability.models import invoke_model


class IntentCategory(str, Enum):
    CONSULTATION = "consultation"
    COMPLAINT = "complaint"
    TRANSACTION = "transaction"
    ACCOUNT = "account"
    COMPLIANCE = "compliance"
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    primary_intent: IntentCategory
    secondary_intent: str
    confidence: float
    entities: dict[str, str]
    suggested_agent: str
    tasks: list[str]=field(default_factory=list)
    slots: dict=field(default_factory=dict)


INTENT_SYSTEM_PROMPT = """你是虚构电商店铺售后客服的意图识别Agent。结合历史对话，只分类最后一条用户消息，不执行历史中的办理请求。当前不办理金融投资、银行开户或真实资金支付。

返回一个JSON对象，不要解释，格式为：
{
  "primary_intent": "consultation|complaint|transaction|account|compliance|unknown",
  "secondary_intent": "具体业务类型",
  "confidence": 0.95,
  "entities": {"ticket_id": "仅在上下文确实出现时提取", "order_id": "同上"},
  "suggested_agent": "knowledge_rag|ticket_handler|compliance_checker",
  "tasks": ["policy_lookup|order_query|order_eligibility|ticket_query|ticket_create|ticket_transition"],
  "slots": {"order_id": null,"ticket_id": null,"requested_amount": null,"type": null,"priority": null,"summary": null,"details": null,"target_status": null,"expected_version": null,"reason": null}
}

路由规则：
- 询问退款政策、条件、到账时间、开户流程/材料或产品信息：knowledge_rag。提到退款或开户不等于申请办理。
- 明确要求申请退款、办理业务、创建/处理投诉工单：ticket_handler。
- 明确申请变更已有工单状态、处理工单或生成状态变更草稿：ticket_handler，primary_intent=transaction，tasks=["ticket_transition"]。从用户提供的工单号、目标状态和当前版本提取 ticket_id、target_status、expected_version 与 reason；生成待确认草稿属于办理请求，不能把“尚未审批”误判为没有办理意图。处理中/已解决/已关闭分别对应 processing/resolved/closed，版本必须为用户明确提供的整数。
- 查询已有工单、询问刚才工单的进度：ticket_handler；从上下文提取真实ticket_id，不要编造。
- 查询本人订单的状态、实付金额、支付/签收时间，或核验某订单的退款申请条件与申请金额：ticket_handler；secondary_intent=order_query 或 refund_eligibility，只提取实际出现的 order_id。这是只读核验，不创建工单。
- 账户异常、资金安全、欺诈举报、索要验证码或可疑操作：compliance_checker。
- 信息不足或无法判断：primary_intent=unknown，降低confidence。

复合请求分解成有限任务。例如“先解释退款政策，再核验订单并申请”返回 tasks=["policy_lookup","order_eligibility","ticket_create"]。依赖和执行由服务器决定，不能跳过前置条件。最多6项，不重复，不包含身份、授权、审批或执行结果。
单个售后任务也可返回对应 tasks；安全咨询或无法确定任务时省略 tasks。只询问政策不能追加写任务。明确办理但缺信息时保留办理任务，在 slots 对缺失信息填null，由服务器询问。
slots仅提取用户已明确提供的信息，金额使用两位小数字符串；不能把订单上限当作申请金额。不从旧历史自动增加新任务。用户回复补充金额/原因时，可只提取slots并用tasks表示正在补充的任务；不要丢失已有待补信息任务。
业务范围之外的金融办理或不相关问题使用unknown。
secondary_intent可以使用refund_policy、shipping_policy、account_help、product_inquiry、ticket_create、ticket_query、security_consultation等。
confidence必须为0到1的数字。只提取有依据的实体；没有实体时返回空对象。
"""


INTENT_SYSTEM_PROMPT += '\n任务提案必须满足以下 JSON Schema（其中 $defs.Slots 是 slots 的字段约束）：\n' + json.dumps(TaskProposal.model_json_schema(), ensure_ascii=False)
INTENT_SYSTEM_PROMPT += '\n状态变更只需要 slots.ticket_id、target_status、expected_version、reason；不要把 ticket_transition 或 resolved 填进 slots.type。没有明确提供的其他槽位请省略或填 null。expected_version 是 JSON 整数，不是字符串。'


class IntentRouterAgent:
    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    @trace_agent_call("intent_router")
    async def classify(self, user_message: str, history: list[BaseMessage] | None = None) -> IntentResult:
        response = await invoke_model(self.llm, [
            SystemMessage(content=INTENT_SYSTEM_PROMPT),
            *(history or []),
            HumanMessage(content=f"请分类这条最新用户消息：{user_message}"),
        ])
        try:
            result = parse_json_object(response.content)
        except ValueError:
            result = {}

        try:
            primary = IntentCategory(result.get("primary_intent", "unknown"))
        except (ValueError, TypeError):
            primary = IntentCategory.UNKNOWN
        raw_confidence = result.get("confidence", 0)
        try:
            confidence = float(raw_confidence) if not isinstance(raw_confidence, bool) else 0.0
        except (ValueError, TypeError):
            confidence = 0.0
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            confidence = 0.0

        route = result.get("suggested_agent", "knowledge_rag")
        if not isinstance(route, str) or route not in {"knowledge_rag", "ticket_handler", "compliance_checker"}:
            route, confidence = "knowledge_rag", 0.0
        secondary = result.get("secondary_intent", "unknown")
        if not isinstance(secondary, str):
            secondary = "unknown"
        raw_entities = result.get("entities", {})
        entities = {
            str(key): str(value) for key, value in raw_entities.items()
            if isinstance(value, (str, int, float)) and not isinstance(value, bool)
        } if isinstance(raw_entities, dict) else {}
        tasks=[];slots={}
        if 'tasks' in result or 'slots' in result:
            from smart_cs.schemas.tasks import TaskProposal,Slots
            try:
                if result.get('tasks') in (None,[]):
                    slots=Slots.model_validate(result.get('slots',{})).model_dump(exclude_none=True)
                else:
                    proposal=TaskProposal.model_validate({'tasks':result['tasks'],'slots':result.get('slots',{})})
                    if len(set(proposal.tasks))!=len(proposal.tasks):raise ValueError()
                    tasks=proposal.tasks;slots=proposal.slots.model_dump(exclude_none=True)
            except (ValueError,TypeError) as error:
                fields=[{'field':'.'.join(str(part) if isinstance(part,int) or part in {'tasks','slots','order_id','ticket_id','requested_amount','type','priority','summary','details','target_status','expected_version','reason'} else '<extra>' for part in item['loc']),'type':item['type']} for item in error.errors()[:5]] if hasattr(error,'errors') else [{'type':type(error).__name__}]
                logging.getLogger(__name__).warning('Intent proposal rejected: %s',fields)
                primary=IntentCategory.UNKNOWN;confidence=0.0
        if not tasks and primary!=IntentCategory.COMPLIANCE and re.search(r'先.{0,150}(?:再|然后|接着).{0,150}(?:申请|登记|创建|关闭|办理)',user_message):
            primary=IntentCategory.UNKNOWN;confidence=0.0
        return IntentResult(primary, secondary, confidence, entities, route,tasks,slots)

    @trace_agent_call("intent_router_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        query, history = latest_user_turn(state.get("messages", []))
        result = await self.classify(query, history) if query else IntentResult(
            IntentCategory.UNKNOWN, "unknown", 0.0, {}, "knowledge_rag"
        )
        details = {
            "primary": result.primary_intent.value,
            "secondary": result.secondary_intent,
            "confidence": result.confidence,
            "entities": result.entities,
            "suggested_agent": result.suggested_agent,
            'tasks':result.tasks,
        }
        return {
            **state,
            "intent": result.suggested_agent,
            "intent_details": details,
            'task_proposal':{'tasks':result.tasks,'slots':result.slots},
            'task_plan':state.get('task_plan',{}) if (state.get('task_plan') or {}).get('status')=='needs_information' else {},
            "sub_results": {"intent_router": details},
            # A new turn replaces the previous turn's execution path.
            "agent_steps": ["intent_router"],
            "agent_models": record_model(state, "intent_router", self.llm, reset=True),
            "current_agent": "intent_router",
        }
