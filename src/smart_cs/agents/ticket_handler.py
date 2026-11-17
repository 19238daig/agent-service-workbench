"""
工单处理Agent — 工单CRUD与流转
负责创建、查询、更新工单，对接工单系统，处理退款/理赔/开户等业务办理类需求。
API 可注入异步数据库仓储；独立离线 Graph 保留内存测试适配器。
"""

from __future__ import annotations

import uuid
import json
import inspect
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from smart_cs.agents.context import append_step, latest_user_turn, record_model
from smart_cs.tracing.otel_config import trace_agent_call
from smart_cs.observability.models import invoke_model
from smart_cs.agents.json_output import parse_json_object
from smart_cs.schemas.orders import Money
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.orders import format_order


class TicketStatus(str, Enum):
    CREATED = "created"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ESCALATED = "escalated"


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


TICKET_SYSTEM_PROMPT = """你是虚构电商店铺售后的工单处理Agent，负责登记申请和查询真实数据库工单，不执行实际支付退款。不能将查询当创建，不能将申请登记当作审批通过。

你的职责：
1. 分析用户需求，判断是否需要创建工单
2. 提取工单关键信息（类型、优先级、描述）
3. 创建工单并返回工单号
4. 查询现有工单状态

工单类型：
- refund: 退款申请
- complaint: 投诉工单
- general: 通用工单

优先级判断规则：
- urgent: 资金安全、账户被盗
- high: 退款超时、理赔争议
- medium: 常规业务办理
- low: 信息咨询类

订单号已明确出现时请通过 order_id 原样返回，不要编造。如果需要先查政策或缺少关键说明，返回 clarify，不要擅自办理。
查询现有工单时，action 必须为 query，并在 JSON 的 ticket_id 字段返回用户提供的完整工单号；不要创建新工单。
只读查询示例：{"action":"query","ticket_id":"用户实际提供的完整工单号"}。此时不需要返回 ticket_type、priority 或 summary 等登记字段。
“查询状态，不要新建/不要修改”仍是 query，不能因否定写操作而改为 create、update 或 clarify。
查询本人订单状态、金额或时间：action=order_query；核验某订单是否可申请退款、可申请金额：action=refund_eligibility。
两者均通过 order_id 原样提取已出现的订单号；缺少订单号时不要编造。不要输出 user_id、实付金额、余额、可退金额或自行计算申请条件，这些由后端服务查询。
仅用户明确给出申请金额时使用 requested_amount，两位小数字符串，如 "10.00"；不得自行补造申请金额。
请只返回 JSON 对象，不要包含额外说明。
请以JSON格式返回工单信息：
{
    "action": "create|query|clarify|order_query|refund_eligibility",
    "ticket_type": "refund|claim|account_open|...",
    "priority": "low|medium|high|urgent",
    "summary": "工单摘要",
    "details": "详细描述"
}
"""


class TicketStore:
    """Only for explicit standalone offline graphs. Never used by live FastAPI."""

    def __init__(self):
        self._tickets: dict[str, dict] = {}

    def create(self, ticket_type: str, priority: str, summary: str, details: str, user_id: str, order_id=None) -> dict:
        ticket_id = f"TK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "priority": priority,
            "status": TicketStatus.CREATED.value,
            "summary": summary,
            "details": details,
            "user_id": user_id,
            "order_id": order_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        self._tickets[ticket_id] = ticket
        return ticket

    def query(self, ticket_id: str, user_id=None) -> dict | None:
        ticket = self._tickets.get(ticket_id)
        return ticket if ticket and (user_id is None or ticket['user_id'] == user_id) else None

    def query_by_user(self, user_id: str) -> list[dict]:
        return [t for t in self._tickets.values() if t["user_id"] == user_id]

    def update_status(self, ticket_id: str, status: str) -> dict | None:
        ticket = self._tickets.get(ticket_id)
        if ticket:
            ticket["status"] = status
            ticket["updated_at"] = datetime.now().isoformat()
        return ticket


class TicketRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    action: Literal['create', 'query', 'clarify', 'update', 'order_query', 'refund_eligibility'] = 'clarify'
    ticket_type: Literal['refund', 'claim', 'account_open', 'account_change', 'complaint', 'general'] = 'general'
    priority: Literal['low', 'medium', 'high', 'urgent'] = 'medium'
    summary: str = Field(default='', max_length=500)
    details: str = Field(default='', max_length=8000)
    ticket_id: str | None = Field(default=None, max_length=40)
    order_id: str | None = Field(default=None, max_length=64)
    requested_amount: Money | None = None


async def store_call(method, *args, **kwargs):
    result = method(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class TicketHandlerAgent:
    """工单处理Agent"""

    def __init__(self, llm: ChatOpenAI, ticket_store: TicketStore | None = None,order_service=None,tool_service=None):
        self.llm = llm
        self.ticket_store = ticket_store or TicketStore()
        self.order_service=order_service
        self.tool_service=tool_service

    @trace_agent_call("ticket_analyze")
    async def analyze_request(self, user_message: str, history: list[BaseMessage] | None = None, intent_details: dict | None = None) -> dict:
        """分析用户需求，提取工单信息"""
        messages = [
            SystemMessage(content=TICKET_SYSTEM_PROMPT),
            *(history or []),
            HumanMessage(content=f"当前用户消息: {user_message}\n意图Agent的分类与实体: " + json.dumps(intent_details or {}, ensure_ascii=False)),
        ]

        response = await invoke_model(self.llm, messages)

        try:
            return parse_json_object(response.content)
        except ValueError:
            return {"action": "clarify"}

    @trace_agent_call("ticket_create")
    async def create_ticket(self, ticket_info: dict, user_id: str) -> str:
        """创建工单"""
        ticket = await store_call(self.ticket_store.create,
            ticket_type=ticket_info.get("ticket_type", "general"),
            priority=ticket_info.get("priority", "medium"),
            summary=ticket_info.get("summary", ""),
            details=ticket_info.get("details", ""),
            user_id=user_id,
            order_id=ticket_info.get('order_id'),
        )

        priority_label = {
            "low": "普通", "medium": "中等", "high": "高", "urgent": "紧急"
        }.get(ticket["priority"], "中等")

        return (
            f"工单已创建成功！\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📝 类型: {ticket['type']}\n"
            f"⚡ 优先级: {priority_label}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n\n"
            f"我们将尽快处理您的请求，请保存好工单号以便后续查询。"
        )

    @trace_agent_call("ticket_query")
    async def query_ticket(self, ticket_id: str, user_id=None) -> str:
        """查询工单状态"""
        ticket = await store_call(self.ticket_store.query, ticket_id, user_id=user_id)
        if not ticket:
            return f"未找到工单号 {ticket_id}，请确认工单号是否正确。"

        status_label = {
            "created": "已创建",
            "processing": "处理中",
            "pending_review": "待审核",
            "resolved": "已解决",
            "closed": "已关闭",
            "escalated": "已升级",
        }.get(ticket["status"], ticket["status"])

        return (
            f"工单查询结果：\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📊 状态: {status_label}\n"
            f"📝 类型: {ticket['type']}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n"
            + (f"\n查询依据：已授权的服务工单记录，版本 {ticket['version']}。\n" if getattr(self.ticket_store,'agent_writes_enabled',True) is False and ticket.get('version') else '')
            + "工单状态仅表示服务处理进度，不代表退款已审批或资金到账。"
            f"🔄 更新时间: {ticket['updated_at']}"
        )

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为Graph节点处理状态"""
        messages = state.get("messages", [])
        user_id = state.get("user_id", "anonymous")

        if not messages:
            return state

        last_message, history = latest_user_turn(messages)
        if self.tool_service is not None:
            from smart_cs.agents.action_planner import plan_turn
            plan,evidence,result=await plan_turn(self.llm,self.tool_service,state,last_message,history)
            return {**state,'action_plan':plan,'ticket_evidence':evidence,'current_agent':'ticket_handler',
                'agent_steps':append_step(state,'ticket_handler'),'agent_models':record_model(state,'ticket_handler',self.llm),
                'sub_results':{**state.get('sub_results',{}),'ticket_handler':result}}
        details = state.get("intent_details", {})
        ticket_info = await self.analyze_request(last_message, history, details)
        if not ticket_info.get("ticket_id"):
            ticket_id = details.get("entities", {}).get("ticket_id")
            if ticket_id:
                ticket_info["ticket_id"] = ticket_id
        if not ticket_info.get('order_id'):
            order_id = details.get('entities', {}).get('order_id')
            if order_id:
                ticket_info['order_id'] = order_id
        try:
            # Validate only fields belonging to the proposed read operation.
            # Irrelevant registration fields (often null/unknown in model output)
            # must neither authorize a write nor reject an otherwise valid read.
            if ticket_info.get('action')=='query':
                ticket_info={key:ticket_info[key] for key in ['action','ticket_id'] if key in ticket_info}
            elif ticket_info.get('action') in {'order_query','refund_eligibility'}:
                ticket_info={key:ticket_info[key] for key in ['action','order_id','requested_amount'] if key in ticket_info}
            ticket_info = TicketRequest.model_validate(ticket_info).model_dump()
        except ValidationError:
            ticket_info = {'action': 'clarify'}

        action = ticket_info.get("action", "clarify")
        # A recognized read-only intent cannot be upgraded to a write by the
        # downstream extraction model. Invalid proposals fall back to clarification.
        if details.get('secondary') in {'order_query','refund_eligibility'} and action in {'create','update'}:
            action='clarify'

        if action in {'order_query','refund_eligibility'}:
            evidence={'action':action,'found':False}
            if self.order_service is None:
                result='订单业务服务尚未接入，不能核实订单事实或申请金额。'
            elif not ticket_info.get('order_id'):
                result='请提供完整订单号，以便只读查询本人订单或核验申请条件；本轮不会创建工单。'
            else:
                from smart_cs.services.access import principal_context
                identity=principal_context.get() or IdentityContext.model_validate(state['identity_context'])
                try:
                    eligibility=await self.order_service.eligibility(identity,ticket_info['order_id'],ticket_info.get('requested_amount')) if action=='refund_eligibility' else None
                    facts=eligibility.facts if eligibility else await self.order_service.get(identity,ticket_info['order_id'])
                    result=format_order(facts,eligibility)
                    evidence.update(found=True,status=facts.status,order_id=facts.order_id,fact_version=facts.fact_version)
                    if eligibility:evidence.update(decision=eligibility.decision,maximum_refund_amount=eligibility.maximum_refund_amount,policy_version=eligibility.policy_version)
                except ApplicationError as error:
                    result=error.message
            return {**state,'ticket_evidence':evidence,'current_agent':'ticket_handler',
                'agent_steps':append_step(state,'ticket_handler'),'agent_models':record_model(state,'ticket_handler',self.llm),
                'sub_results':{**state.get('sub_results',{}),'ticket_handler':result}}

        if action == "query":
            result = await self.query_ticket(ticket_info["ticket_id"], user_id=user_id) if ticket_info.get("ticket_id") else "请提供完整工单号，以便查询工单状态。"
        elif action == "create":
            if not getattr(self.ticket_store,'agent_writes_enabled',True):
                result='工单尚未创建。当前聊天仅支持查询；如需登记，请在[人工登记接口](/docs)确认提交。登记仅记录服务申请，不会自动退款。'
            else:
                try:
                    result = await self.create_ticket(ticket_info, user_id)
                except ValueError:
                    result = '工单未创建，请核实本人工作区的订单和申请信息后重试。'
        else:
            result = "请说明要查询订单、核验申请条件还是登记工单；只读核验不会自动创建工单。"

        evidence = {"action": action if action in {"query", "create"} else "clarify"}
        if action=='create' and not getattr(self.ticket_store,'agent_writes_enabled',True):evidence['write_blocked']=True
        if action == "create":
            # Match only an ID that actually exists in the store, never a model claim.
            import re
            match = re.search(r"TK-\d{8}-[0-9A-F]{6}", result)
            ticket_id = match.group(0) if match else None
            if ticket_id is None:
                evidence['found'] = False
        else:
            ticket_id = ticket_info.get("ticket_id")
        if ticket_id:
            record = await store_call(self.ticket_store.query, ticket_id, user_id=user_id)
            evidence.update(ticket_id=ticket_id, found=record is not None)
            if record:
                evidence["status"] = record["status"]

        return {
            **state,
            "ticket_evidence": evidence,
            "current_agent": "ticket_handler",
            "agent_steps": append_step(state, "ticket_handler"),
            "agent_models": record_model(state, "ticket_handler", self.llm),
            "sub_results": {
                **state.get("sub_results", {}),
                "ticket_handler": result,
            },
        }
