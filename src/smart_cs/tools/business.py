"""Typed planning contracts; direct model calls remain read-only.

Protected writes use the separate durable, approved-action execution service.
"""
from smart_cs.schemas.actions import (OrderArguments,EligibilityArguments,TicketArguments,
    TicketListArguments,TicketEventsArguments,CreateArguments,TransitionArguments)

ARGUMENTS={
    'order_query':OrderArguments,'order_eligibility':EligibilityArguments,
    'ticket_query':TicketArguments,'ticket_list':TicketListArguments,'ticket_events':TicketEventsArguments,
    'ticket_create':CreateArguments,'ticket_transition':TransitionArguments,
}
READ_TOOLS=frozenset({'order_query','order_eligibility','ticket_query','ticket_list','ticket_events'})
DESCRIPTIONS={
    'order_query':'只读查询本人订单；省略订单号时分页列出本人订单',
    'order_eligibility':'只读核验本人订单申请条件和金额上限，不创建工单、不退款',
    'ticket_query':'只读查询本人或已分派给当前客服的工单',
    'ticket_list':'分页列出有权访问的工单；assigned 队列限客服/管理员',
    'ticket_events':'只读查询有权访问工单的事件记录',
    'ticket_create':'仅提出登记草稿；退款需本人订单和明确申请金额；执行由服务端确认与审批流程处理',
    'ticket_transition':'仅提出流转草稿；检查工单版本及允许转换；执行由服务端确认与审批流程处理',
}


def tool_catalog(*,protected_execution=False):
    return [{'name':name,'description':('仅通过已确认、已审批的动作ID执行此写工具；原始参数不能直接执行' if protected_execution and name not in READ_TOOLS else DESCRIPTIONS[name]),'read_only':name in READ_TOOLS,
        'execution_enabled':name in READ_TOOLS or protected_execution,'input_schema':model.model_json_schema(),
        'execution_mode':'read' if name in READ_TOOLS else 'approved_action_only' if protected_execution else 'draft_only',
        'protocol':'action-plan-v1'} for name,model in ARGUMENTS.items()]
