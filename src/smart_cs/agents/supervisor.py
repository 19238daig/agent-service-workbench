"""Coordinate four business agents through an explicit LangGraph workflow."""
from __future__ import annotations

from typing import Annotated, Any, Mapping, TypedDict

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from smart_cs.agents.compliance_checker import ComplianceCheckerAgent
from smart_cs.agents.context import append_step
from smart_cs.agents.intent_router import IntentRouterAgent
from smart_cs.agents.knowledge_rag import KnowledgeRAGAgent
from smart_cs.agents.ticket_handler import TicketHandlerAgent
from smart_cs.models import build_agent_models
from smart_cs.memory.long_term import LongTermMemory
from smart_cs.memory.short_term import ShortTermMemory
from smart_cs.memory.working_memory import WorkingMemory
from smart_cs.tracing.otel_config import trace_agent_call


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    identity_context: dict[str, Any]
    request_id: str
    trace_id: str
    run_id: str
    turn_id: str
    session_id: str
    intent: str
    intent_details: dict[str, Any]
    agent_steps: list[str]
    agent_models: dict[str, str]
    sub_results: dict[str, Any]
    knowledge_evidence: dict[str, Any]
    ticket_evidence: dict[str, Any]
    action_plan: dict[str, Any]
    action_review: dict[str, Any]
    action_request: dict[str, Any]
    action_execution: dict[str, Any]
    task_plan: dict[str, Any]
    task_proposal: dict[str, Any]
    task_result: dict[str, Any]
    business_context_fingerprint: str
    content_review: dict[str, Any]
    review_protocol: str
    compliance_passed: bool
    final_response: str
    current_agent: str
    retry_count: int


class SupervisorNode:
    def __init__(self, working_memory: WorkingMemory):
        self.working_memory = working_memory

    @trace_agent_call("supervisor")
    async def route_decision(self, state: AgentState) -> AgentState:
        """Use the intent agent's result without a duplicate model call."""
        details = state.get("intent_details", {})
        intent = details.get("suggested_agent", "knowledge_rag")
        confidence = details.get("confidence", 0)
        results = dict(state.get("sub_results", {}))
        if details.get("primary", "unknown") == "unknown" or confidence < 0.5:
            intent = "clarification"
            results["supervisor"] = "您是想了解业务政策、提交业务申请，还是查询已有工单？请补充具体需求，我会继续为您处理。"
        elif details.get("primary") == "compliance":
            intent = "compliance_checker"
        elif intent not in {"knowledge_rag", "ticket_handler", "compliance_checker"}:
            intent = "knowledge_rag"
        self.working_memory.update(state.get("session_id", "default"), {
            "last_intent": intent,
            "intent_details": details,
        })
        return {
            **state, "intent": intent, "sub_results": results,
            "current_agent": "supervisor", "agent_steps": append_step(state, "supervisor_route"),
        }

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> AgentState:
        if state.get('review_protocol')=='dual-review-v1':
            from smart_cs.services.compliance import release_allowed
            state={**state,'compliance_passed':bool(state.get('compliance_passed')) and release_allowed(state)}
        if not state.get("compliance_passed", True):
            final_response = "这条回复需要进一步核实。请通过官方渠道联系人工客服处理。"
        else:
            parts = [value for value in state.get("sub_results", {}).values() if isinstance(value, str) and value.strip()]
            final_response = "\n\n".join(parts) if parts else "暂时无法处理这个问题，请补充信息后重试。"
        return {
            **state, "final_response": final_response,
            "messages": [AIMessage(content=final_response)], "current_agent": "supervisor",
            "agent_steps": append_step(state, "synthesize"),
        }


def route_to_agent(state: AgentState) -> str:
    return {
        "knowledge_rag": "knowledge_rag", "ticket_handler": "ticket_handler",
        "compliance_checker": "compliance_check", "clarification": "compliance_check",
    }.get(state.get("intent", "knowledge_rag"), "knowledge_rag")


def create_supervisor_graph(
    llm: BaseChatModel | None = None,
    working_memory: WorkingMemory | None = None,
    short_term_memory: ShortTermMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    enable_checkpointing: bool = True,
    agent_llms: Mapping[str, BaseChatModel] | None = None,
    ticket_store=None,
    order_service=None,
    checkpointer=None,
    tool_service=None,
    approval_service=None,
    execution_service=None,
):
    """Classify → dispatch → handle → review → synthesize, with per-session checkpoints."""
    models = build_agent_models(default_model=llm, overrides=agent_llms)
    supervisor = SupervisorNode(working_memory or WorkingMemory())
    intent_router = IntentRouterAgent(models["intent_router"])
    knowledge = KnowledgeRAGAgent(models["knowledge_rag"], long_term_memory)
    tickets = TicketHandlerAgent(models["ticket_handler"], ticket_store=ticket_store,order_service=order_service,tool_service=tool_service)
    compliance = ComplianceCheckerAgent(models["compliance_checker"],tool_service=tool_service,execution_service=execution_service)

    from smart_cs.services.tasks import TaskOrchestrator
    orchestrator=TaskOrchestrator(tool_service,models['ticket_handler']) if tool_service is not None and execution_service is not None else None
    graph = StateGraph(AgentState)
    from smart_cs.persistence.context import execution_binding,GRAPH_VERSION
    from smart_cs.schemas.errors import ApplicationError,classify_failure
    from langgraph.errors import GraphBubbleUp
    def boundary(process):
        async def execute(state):
            state={**state,'review_protocol':'dual-review-v1' if tool_service is not None else 'legacy_demo'}
            binding=execution_binding.get()
            if binding:state={**state,**binding}
            try:return await process(state)
            except GraphBubbleUp:raise
            except Exception as error:
                if checkpointer is None:raise
                code,status,message=classify_failure(error)
                raise ApplicationError(code,message,status) from None
        return execute
    async def business_process(state):
        if orchestrator is not None and state.get('task_plan'):return await orchestrator.business(state)
        return await tickets.process(state)
    async def knowledge_process(state):
        if orchestrator is not None and state.get('task_plan'):
            from smart_cs.services.tasks import policy_query
            from langchain_core.messages import HumanMessage
            focused={**state,'messages':[HumanMessage(content=policy_query(state['task_plan']['goal']))]}
            result=await knowledge.process(focused)
            return {**result,'messages':state.get('messages',[])}
        return await knowledge.process(state)
    for name,process in [('intent_router',intent_router.process),('supervisor_route',supervisor.route_decision),
        ('knowledge_rag',knowledge_process),('ticket_handler',business_process),('compliance_check',compliance.process),('synthesize',supervisor.synthesize_response)]:
        graph.add_node(name,boundary(process))
    graph.set_entry_point("intent_router")
    graph.add_edge("intent_router", "supervisor_route")
    def dispatch_route(state):
        kinds=(state.get('task_proposal') or {}).get('tasks') or []
        use_tasks=len(kinds)>1 or any(k in {'ticket_create','ticket_transition'} for k in kinds)
        return 'task_initialize' if orchestrator is not None and (use_tasks or (state.get('task_plan') or {}).get('status')=='needs_information') else route_to_agent(state)
    graph.add_conditional_edges("supervisor_route", dispatch_route, {
        "knowledge_rag": "knowledge_rag", "ticket_handler": "ticket_handler", "compliance_check": "compliance_check",
        **({'task_initialize':'task_initialize'} if orchestrator is not None else {}),
    })
    if orchestrator is not None:
        graph.add_node('task_initialize',boundary(orchestrator.initialize))
        graph.add_node('task_dispatch',boundary(orchestrator.dispatch))
        graph.add_node('task_collect',boundary(orchestrator.collect))
        graph.add_node('task_after_review',boundary(orchestrator.after_review))
        graph.add_edge('task_initialize','task_dispatch')
        graph.add_conditional_edges('task_dispatch',orchestrator.route,{'knowledge_rag':'knowledge_rag','ticket_handler':'ticket_handler','finish':'action_review'})
        graph.add_conditional_edges('task_collect',lambda s:'continue' if s['task_plan']['status']=='running' else 'finish',{'continue':'task_dispatch','finish':'action_review'})
        graph.add_conditional_edges('knowledge_rag',lambda s:'task_collect' if s.get('task_plan') else 'compliance_check',{'task_collect':'task_collect','compliance_check':'compliance_check'})
    else:graph.add_edge("knowledge_rag", "compliance_check")
    if tool_service is not None:
        graph.add_node('action_review',boundary(compliance.review_action))
        if orchestrator is not None:
            graph.add_conditional_edges('ticket_handler',lambda s:'task_collect' if s.get('task_plan') else 'action_review',{'task_collect':'task_collect','action_review':'action_review'})
        else:graph.add_edge('ticket_handler','action_review')
        graph.add_edge('action_review','compliance_check')
    else:graph.add_edge("ticket_handler", "compliance_check")
    if orchestrator is not None:
        graph.add_edge('compliance_check','task_after_review');graph.add_edge('task_after_review','synthesize')
    else:graph.add_edge("compliance_check", "synthesize")
    if approval_service is not None:
        from smart_cs.services.approvals import ApprovalGate
        graph.add_node('approval_gate',boundary(ApprovalGate(approval_service)))
        graph.add_edge('synthesize','approval_gate')
        if execution_service is not None:
            from smart_cs.services.execution import ExecutionNode
            graph.add_node('execute_action',boundary(ExecutionNode(execution_service)))
            graph.add_node('execution_review',boundary(compliance.review_execution))
            graph.add_node('execution_synthesize',boundary(supervisor.synthesize_response))
            graph.add_edge('approval_gate','execute_action')
            graph.add_edge('execute_action','execution_review')
            graph.add_edge('execution_review','execution_synthesize')
            graph.add_edge('execution_synthesize',END)
        else:graph.add_edge('approval_gate',END)
    else: graph.add_edge("synthesize", END)
    compiled = graph.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver() if enable_checkpointing else None)
    from smart_cs.persistence.conversations import digest
    model_settings={role:{'model':str(getattr(model,'model_name',None) or type(model).__name__),
        'endpoint':str(getattr(model,'openai_api_base',None) or ''),'temperature':getattr(model,'temperature',None),
        'max_tokens':getattr(model,'max_tokens',None),'timeout':getattr(model,'request_timeout',None)} for role,model in models.items()}
    return compiled.with_config(recursion_limit=64,metadata={'graph_version':GRAPH_VERSION,'workflow_model_fingerprint':digest(model_settings),
        'review_protocol':'dual-review-v1' if tool_service is not None else 'legacy_demo',
        'protected_execution_protocol':'action-execution-v1' if execution_service is not None else None,
        'task_protocol':'finite-tasks-v1' if orchestrator is not None else None,
        'agent_writes_enabled':True if execution_service is not None else False if tool_service is not None else getattr(ticket_store,'agent_writes_enabled',True),"workflow_models": {
        role: str(getattr(model, "model_name", None) or type(model).__name__)
        for role, model in models.items()
    }})
