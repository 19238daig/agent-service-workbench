"""Ticket business rules. The Agent receives only a read port, never this writer."""
from pydantic import TypeAdapter,ValidationError
from smart_cs.schemas.tickets import TicketView,TicketEventView,TicketId,TicketRegistration,TicketTransition,TicketAssignment
from smart_cs.schemas.errors import ApplicationError
from smart_cs.repositories.tickets import missing

TRANSITIONS={
    'created':frozenset({'processing','pending_review','escalated','closed'}),
    'processing':frozenset({'pending_review','resolved','escalated'}),
    'pending_review':frozenset({'processing','resolved','escalated'}),
    'escalated':frozenset({'processing','resolved'}),'resolved':frozenset({'closed'}),'closed':frozenset(),
}
ACTIVE=frozenset({'created','processing','pending_review','escalated'})


def ticket_identifier(value):
    try:return TypeAdapter(TicketId).validate_python(value)
    except ValidationError:raise ApplicationError('INVALID_TICKET_ID','工单号格式无效。',422) from None


def view(row,identity):
    result=TicketView.model_validate(row)
    allowed=set()
    if row.user_id==identity.subject_id and row.status in {'created','resolved'}:allowed.add('closed')
    if identity.role in {'support','admin'} and row.assignee_id==identity.subject_id:allowed.update(TRANSITIONS[row.status])
    return result.model_copy(update={'allowed_transitions':sorted(allowed),'can_assign':row.user_id==identity.subject_id and row.status in ACTIVE})


def pagination(limit,offset):
    if type(limit) is not int or type(offset) is not int or not 1<=limit<=100 or offset<0:
        raise ApplicationError('INVALID_PAGINATION','分页参数无效。',422)


class TicketService:
    def __init__(self,repository):self.repository=repository

    async def get(self,identity,ticket_id):
        row=await self.repository.get(identity,ticket_identifier(ticket_id))
        if row is None:raise missing()
        return view(row,identity)

    async def list(self,identity,limit=50,offset=0,assigned=False,status=None):
        pagination(limit,offset)
        if assigned and identity.role not in {'support','admin'}:raise ApplicationError('FORBIDDEN','此队列仅供客服处理已分派工单。',403)
        if status is not None and status not in TRANSITIONS:raise ApplicationError('INVALID_STATUS','工单状态无效。',422)
        rows,total=await self.repository.list(identity,limit,offset,assigned,status)
        return {'demo_only':True,'tickets':[view(row,identity).model_dump(mode='json') for row in rows],
            'total':total,'limit':limit,'offset':offset,'has_more':offset+len(rows)<total}

    async def events(self,identity,ticket_id,limit=50,offset=0):
        pagination(limit,offset);await self.get(identity,ticket_id)
        rows,total=await self.repository.events(identity,ticket_id,limit,offset)
        return {'ticket_id':ticket_id,'events':[TicketEventView.model_validate(row).model_dump(mode='json') for row in rows],
            'total':total,'limit':limit,'offset':offset,'has_more':offset+len(rows)<total}

    async def register(self,identity,body:TicketRegistration):
        if not identity.authenticated:raise ApplicationError('UNAUTHENTICATED','人工登记需要有效登录。',401)
        if body.type not in {'refund','complaint','general'}:raise ApplicationError('UNSUPPORTED_BUSINESS','当前仅登记电商售后、投诉和通用服务申请。',422)
        if body.type=='refund' and not body.order_id:raise ApplicationError('ORDER_REQUIRED','退款申请登记需要提供本人订单号。',422)
        async with self.repository.transaction() as unit:row=await unit.create(identity,body,'manual_api')
        return view(row,identity)

    async def assign(self,identity,ticket_id,body:TicketAssignment):
        if not identity.authenticated:raise ApplicationError('UNAUTHENTICATED','工单分派需要有效登录。',401)
        async with self.repository.transaction() as unit:
            row=await unit.get(identity,ticket_identifier(ticket_id))
            if row.user_id!=identity.subject_id:raise ApplicationError('FORBIDDEN','只有工单本人可以选择或撤销客服分派。',403)
            if row.version!=body.expected_version:raise ApplicationError('TICKET_VERSION_CONFLICT','工单已更新，请重新读取后提交。',409)
            if row.status not in ACTIVE:raise ApplicationError('TICKET_ASSIGNMENT_NOT_ALLOWED','已解决或关闭的工单不能重新分派。',409)
            if body.assignee_id and not await unit.assignable_staff(body.assignee_id):raise ApplicationError('INVALID_ASSIGNEE','请选择当前可用的客服人员。',422)
            if row.assignee_id==body.assignee_id:raise ApplicationError('ASSIGNMENT_UNCHANGED','分派没有变化，请勿重复提交。',409)
            row=await unit.change(row,identity,body.expected_version,'assigned' if body.assignee_id else 'unassigned',body.reason,
                change_assignee=True,assignee_id=body.assignee_id)
        return view(row,identity)

    async def transition(self,identity,ticket_id,body:TicketTransition):
        if not identity.authenticated:raise ApplicationError('UNAUTHENTICATED','工单流转需要有效登录。',401)
        async with self.repository.transaction() as unit:
            row=await unit.get(identity,ticket_identifier(ticket_id))
            if row.source=='approved_action':raise ApplicationError('ACTION_APPROVAL_REQUIRED','请通过动作草稿确认与审批流程变更此工单。',403)
            permitted=(identity.role in {'support','admin'} and row.assignee_id==identity.subject_id) or (
                row.user_id==identity.subject_id and row.status in {'created','resolved'} and body.target_status=='closed')
            if not permitted:raise ApplicationError('FORBIDDEN','当前账号无权执行此工单流转。',403)
            if row.version!=body.expected_version:raise ApplicationError('TICKET_VERSION_CONFLICT','工单已更新，请重新读取后提交。',409)
            if body.target_status not in TRANSITIONS[row.status]:raise ApplicationError('TICKET_TRANSITION_NOT_ALLOWED','当前工单状态不允许此转换。',409)
            row=await unit.change(row,identity,body.expected_version,'status_changed',body.reason,target_status=body.target_status)
        return view(row,identity)


class AgentTicketReader:
    agent_writes_enabled=False
    def __init__(self,service):self.service=service

    async def query(self,ticket_id,user_id=None):
        from smart_cs.services.access import principal_context
        identity=principal_context.get()
        if identity is None or not identity.authenticated:raise ApplicationError('UNAUTHENTICATED','工单查询需要服务端验证的身份。',401)
        if self.service is None:raise ApplicationError('TICKET_SERVICE_UNAVAILABLE','工单服务尚未就绪。',503)
        try:return (await self.service.get(identity,ticket_id)).model_dump(mode='json')
        except ApplicationError as error:
            if error.status_code==404:return None
            raise
