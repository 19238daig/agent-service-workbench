"""Authenticated plan validation and read dispatch; no business write calls."""
from decimal import Decimal
from uuid import uuid4,uuid5,NAMESPACE_URL
from smart_cs.db.models import utcnow
from smart_cs.schemas.actions import ActionPlan,PlanValidation,canonical_hash
from smart_cs.schemas.errors import ApplicationError
from smart_cs.tools.business import READ_TOOLS


class ActionPlanService:
    def __init__(self,orders,tickets,*,clock=utcnow):self.orders=orders;self.tickets=tickets;self.clock=clock

    @staticmethod
    def authenticated(identity):
        if identity is None or not identity.authenticated:raise ApplicationError('UNAUTHENTICATED','工具调用需要有效登录。',401)

    async def assess(self,identity,plan:ActionPlan,*,binding=None):
        self.authenticated(identity)
        # Revalidate even when an internal caller passes a constructed model.
        plan=ActionPlan.model_validate(plan.model_dump())
        call=plan.action;args=call.arguments;params=args.model_dump(mode='json')
        fingerprint=canonical_hash({'protocol':plan.protocol,'actor':identity.subject_id,'role':identity.role,'tool':call.name,'parameters':params})
        action_id='act-'+uuid5(NAMESPACE_URL,f'smart-cs:{identity.subject_id}:{binding or uuid4().hex}:{fingerprint}').hex
        result=PlanValidation(action_id=action_id,actor_id=identity.subject_id,tool=call.name,
            read_only=call.name in READ_TOOLS,status='read_ready' if call.name in READ_TOOLS else 'draft_validated',
            parameters=params,parameter_hash=fingerprint,reason_codes=[],validated_at=self.clock().isoformat(),execution_enabled=call.name in READ_TOOLS)
        data=None
        try:
            if call.name=='order_query':
                if args.order_id:
                    facts=await self.orders.get(identity,args.order_id)
                    data=[facts.model_dump(mode='json')];result.fact_versions={f'order:{facts.order_id}':facts.fact_version}
                else:data=[r.model_dump(mode='json') for r in (await self.orders.list(identity,args.limit,args.offset)).orders]
            elif call.name=='order_eligibility':
                eligibility=await self.orders.eligibility(identity,args.order_id,args.requested_amount)
                data=eligibility.model_dump(mode='json');result.fact_versions={f'order:{args.order_id}':eligibility.fact_version};result.policy_version=eligibility.policy_version
            elif call.name=='ticket_query':
                ticket=await self.tickets.get(identity,args.ticket_id)
                data=ticket.model_dump(mode='json');result.fact_versions={f'ticket:{ticket.ticket_id}':ticket.version}
            elif call.name=='ticket_list':data=await self.tickets.list(identity,args.limit,args.offset,args.assigned,args.status)
            elif call.name=='ticket_events':data=await self.tickets.events(identity,args.ticket_id,args.limit,args.offset)
            elif call.name=='ticket_create':
                if args.type=='refund' and args.requested_amount is not None:
                    result.effects={'refund_reserved_delta':args.requested_amount,'funds_refunded':False}
                if args.requested_amount is not None and Decimal(args.requested_amount)<=0:
                    raise ApplicationError('INVALID_AMOUNT','申请金额必须大于零。',422)
                result.missing_fields=[k for k in ['summary','details'] if not getattr(args,k)]
                if args.type!='refund' and args.requested_amount is not None:
                    raise ApplicationError('AMOUNT_NOT_APPLICABLE','只有退款申请可以指定申请金额。',422)
                if args.type=='refund':
                    result.missing_fields += [k for k in ['order_id','requested_amount'] if getattr(args,k) is None]
                # Verify supplied resources even if other slots are missing.
                if args.order_id:
                    facts=await self.orders.get(identity,args.order_id)
                    result.fact_versions={f'order:{facts.order_id}':facts.fact_version}
                    if args.type=='refund':
                        eligibility=await self.orders.eligibility(identity,args.order_id,args.requested_amount)
                        result.policy_version=eligibility.policy_version
                        if eligibility.decision=='denied':
                            result.status='rejected';result.reason_codes=list(eligibility.reason_codes)
                        elif eligibility.decision in {'requires_review','needs_information'}:
                            result.status='needs_information';result.reason_codes=list(eligibility.reason_codes)
                if result.missing_fields and result.status!='rejected':result.status='needs_information';result.reason_codes.append('REQUIRED_SLOTS_MISSING')
            elif call.name=='ticket_transition':
                ticket=await self.tickets.get(identity,args.ticket_id)
                result.fact_versions={f'ticket:{ticket.ticket_id}':ticket.version}
                if args.target_status=='closed' and ticket.source=='approved_action' and ticket.requested_amount is not None:
                    result.effects={'refund_reserved_delta':format(-ticket.requested_amount,'.2f'),'funds_refunded':False}
                if args.expected_version!=ticket.version:raise ApplicationError('TICKET_VERSION_CONFLICT','工单版本已变化，请重新读取。',409)
                if args.target_status not in ticket.allowed_transitions:raise ApplicationError('TICKET_TRANSITION_NOT_ALLOWED','当前身份或工单状态不允许此转换。',403)
            if not result.read_only:result.reason_codes.append('WRITE_EXECUTION_DISABLED')
        except ApplicationError as error:
            if error.status_code not in {403,404,409,422}:raise
            result.status='rejected';result.reason_codes=[error.code]
            result.execution_enabled=False
            data=None
        return result,data

    async def validate(self,identity,plan,*,binding=None):
        return (await self.assess(identity,plan,binding=binding))[0]

    async def call_read(self,identity,name,arguments):
        self.authenticated(identity)
        # Fail before parsing or touching any service for a write/unknown tool.
        if name not in READ_TOOLS:raise ApplicationError('TOOL_FORBIDDEN','此工具没有直接执行入口。',403)
        from pydantic import ValidationError
        try:plan=ActionPlan(action={'name':name,'arguments':arguments})
        except ValidationError:raise ApplicationError('INVALID_TOOL_ARGUMENTS','工具参数不符合类型化契约。',422) from None
        result,data=await self.assess(identity,plan)
        if result.status=='rejected':
            code=result.reason_codes[0]
            if name=='order_query' and code=='ORDER_NOT_FOUND':return []
            raise ApplicationError(code,'当前账号不能完成此工具查询，请核实资源与参数。',404 if code.endswith('NOT_FOUND') else 403)
        return data


def format_plan(result,*,approval=None):
    if result.status=='rejected':return '操作计划未通过校验，未执行任何写入。请核实本人订单、工单及申请条件。原因：'+', '.join(result.reason_codes)
    if result.status=='needs_information':return '操作尚未执行。请补充或核实：'+', '.join(result.missing_fields or result.reason_codes)+'。本轮不会创建工单或退款。'
    labels={'ticket_create':'工单登记','ticket_transition':'工单状态变更'}
    pending='后续仍需动作审查、确认/审批与执行前重新校验；此草稿和动作 ID 不构成执行授权。'
    if approval in {'user','staff'}:
        pending='动作风险审查已完成，后续仍需'+('用户确认' if approval=='user' else '人工客服审批')+'与执行前重新校验；当前未获得审批或执行授权。'
    return '\n\n'.join([f'已生成{labels.get(result.tool,result.tool)}草稿，尚未创建或修改工单，也未退款。',
        '动作 ID：'+result.action_id,'草稿参数：',*[f'{k}：{v}' for k,v in result.parameters.items() if v is not None],
        *(['执行登记时会占用本次申请额度，关闭该申请后释放；不会实际退款。'] if result.tool=='ticket_create' and result.parameters.get('type')=='refund' else []),
        *(['关闭受保护的退款申请会释放该工单登记时占用的额度。'] if result.tool=='ticket_transition' and result.parameters.get('target_status')=='closed' else []),
        '校验版本：'+str(result.fact_versions),pending])
