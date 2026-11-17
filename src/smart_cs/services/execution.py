"""One database transaction per approved action; a durable receipt is the retry key.

No LLM, network call or output review is inside this transaction. PostgreSQL row
locks serialize action replays and competing refunds. Fault injection is a Python
test seam only, never a request field or environment switch in the application.
"""
from decimal import Decimal
from sqlalchemy import select
from smart_cs.db.models import ActionRequest,ActionExecution,Approval,Ticket,TicketEvent,Order,User,AuthCredential,utcnow
from smart_cs.schemas.actions import ActionPlan,PlanValidation,canonical_hash
from smart_cs.schemas.execution import ExecutionResult,PROTOCOL
from smart_cs.schemas.tickets import TicketRegistration
from smart_cs.services.approvals import ApprovalService,aware,failure
from smart_cs.repositories.orders import order_facts
from smart_cs.repositories.tickets import TicketUnitOfWork,visibility
from smart_cs.services.actions import ActionPlanService
from smart_cs.services.orders import OrderService
from smart_cs.services.tickets import TicketService


class TransactionOrderReader:
    def __init__(self,session):self.session=session
    async def get(self,identifier,owner):
        row=await self.session.scalar(select(Order).where(Order.id==identifier,Order.user_id==owner).with_for_update())
        return order_facts(row) if row else None


class TransactionTicketReader:
    def __init__(self,session):self.session=session
    async def get(self,identity,identifier):
        return await self.session.scalar(select(Ticket).where(Ticket.id==identifier,visibility(identity)).with_for_update())


class ActionExecutionService:
    def __init__(self,database,clock=utcnow,*,fault=None):self.database=database;self.clock=clock;self.fault=fault

    async def checkpoint(self,name):
        if self.fault is not None:await self.fault(name)

    async def actor(self,session,identity):
        if not identity or not identity.authenticated:raise failure('AUTHENTICATION_REQUIRED',status=401)
        user=await session.get(User,identity.subject_id)
        credential=await session.get(AuthCredential,identity.subject_id)
        if not user or user.role!=identity.role or (credential and not credential.enabled):raise failure('AUTHORIZATION_CHANGED',status=403)

    async def receipt(self,session,request):
        row=await session.get(ActionExecution,request.id)
        if row is None:return None
        try:
            result=ExecutionResult.model_validate(row.result)
            event=await session.get(TicketEvent,row.event_id)
            if (canonical_hash(row.result)!=row.result_hash or canonical_hash(request.snapshot)!=request.draft_hash
                or row.user_id!=request.user_id or row.parameter_hash!=request.parameter_hash or row.draft_hash!=request.draft_hash
                or result.action_id!=request.id or result.parameter_hash!=request.parameter_hash or result.draft_hash!=request.draft_hash
                or result.tool!=request.tool or row.tool!=request.tool or result.ticket_id!=row.ticket_id or result.event_id!=row.event_id
                or not event or event.action_id!=request.id or event.ticket_id!=row.ticket_id or event.actor_id!=request.user_id
                or event.ticket_version!=result.ticket_version or event.to_status!=result.ticket_status):raise ValueError()
        except (ValueError,TypeError,KeyError):raise failure('EXECUTION_RECEIPT_INVALID','执行记录需要核实，已停止自动重试。',503) from None
        return result.model_dump(mode='json')

    async def get(self,identity,identifier):
        async with self.database.sessions() as session:
            await self.actor(session,identity)
            row=await session.get(ActionRequest,identifier);ApprovalService.authorize(self,row,identity)
            result=await self.receipt(session,row)
            if result is None:raise failure('ACTION_RESULT_NOT_FOUND','此动作尚无已提交的执行结果。',404)
            return result

    async def for_turn(self,identity,turn_id):
        async with self.database.sessions() as session:
            row=await session.scalar(select(ActionRequest).where(ActionRequest.turn_id==turn_id,ActionRequest.user_id==identity.subject_id))
            if row is None:return None
            return await self.receipt(session,row)

    async def execute(self,identity,identifier,parameter_hash):
        result=None;error=None;new_commit=False
        async with self.database.sessions.begin() as session:
            from smart_cs.durable.store import check_attempt
            await check_attempt(session,identity)
            request=await session.scalar(select(ActionRequest).where(ActionRequest.id==identifier).with_for_update())
            ApprovalService.authorize(self,request,identity)
            if parameter_hash!=request.parameter_hash:raise failure('ACTION_BINDING_MISMATCH')
            # Lock all authorizing principals in stable order, before business facts.
            principals=set((await session.scalars(select(Approval.principal_id).where(Approval.action_id==identifier))).all())|{identity.subject_id}
            await session.execute(select(User.id).where(User.id.in_(sorted(principals))).order_by(User.id).with_for_update())
            await session.execute(select(AuthCredential.user_id).where(AuthCredential.user_id.in_(sorted(principals))).order_by(AuthCredential.user_id).with_for_update())
            await self.actor(session,identity)
            result=await self.receipt(session,request)
            if result is None:
                plan=PlanValidation.model_validate(request.snapshot['action_plan'])
                arguments=ActionPlan(action={'name':plan.tool,'arguments':plan.parameters}).action.arguments
                ticket=None;order=None
                order_id=arguments.order_id if plan.tool=='ticket_create' else None
                if plan.tool=='ticket_transition':
                    preliminary=await session.scalar(select(Ticket).where(Ticket.id==arguments.ticket_id,visibility(identity)))
                    if preliminary is None:raise failure('ACTION_STALE')
                    order_id=preliminary.order_id
                if order_id:
                    order=await session.scalar(select(Order).where(Order.id==order_id).with_for_update())
                if plan.tool=='ticket_transition':
                    ticket=await TicketUnitOfWork(session).get(identity,arguments.ticket_id)
                    if ticket.order_id!=order_id:raise failure('ACTION_STALE')
                actions=ActionPlanService(OrderService(TransactionOrderReader(session),clock=self.clock),TicketService(TransactionTicketReader(session)),clock=self.clock)
                approvals=ApprovalService(self.database,actions,clock=self.clock)
                error=await approvals.freshness(session,request,approvals=True)
                if not error:
                    # No model decision or caller-selected parameters enter this block.
                    unit=TicketUnitOfWork(session);change=Decimal('0.00')
                    if plan.tool=='ticket_create':
                        amount=Decimal(arguments.requested_amount) if arguments.type=='refund' else None
                        if amount is not None:
                            order.refund_reserved_amount+=amount;order.fact_version+=1;change=amount
                        body=TicketRegistration(type=arguments.type,priority=arguments.priority,summary=arguments.summary,
                            details=arguments.details,order_id=arguments.order_id,confirm_registration=True)
                        ticket=await unit.create(identity,body,'approved_action',action_id=identifier,requested_amount=amount)
                    else:
                        if arguments.target_status=='closed' and ticket.source=='approved_action' and ticket.requested_amount is not None:
                            amount=ticket.requested_amount
                            if not order or order.user_id!=ticket.user_id or order.refund_reserved_amount is None or order.refund_reserved_amount<amount:
                                raise failure('REFUND_RESERVATION_INVALID')
                            order.refund_reserved_amount-=amount;order.fact_version+=1;change=-amount
                        ticket=await unit.change(ticket,identity,arguments.expected_version,'status_changed',arguments.reason,
                            source='approved_action',target_status=arguments.target_status,action_id=identifier)
                    await session.flush()
                    event=await session.scalar(select(TicketEvent).where(TicketEvent.action_id==identifier))
                    now=self.clock()
                    result=ExecutionResult(action_id=identifier,parameter_hash=request.parameter_hash,draft_hash=request.draft_hash,
                        tool=plan.tool,ticket_id=ticket.id,ticket_version=ticket.version,ticket_status=ticket.status,event_id=event.id,
                        committed_at=aware(now).isoformat(),requested_amount=format(ticket.requested_amount,'.2f') if ticket.requested_amount is not None else None,
                        reservation_change=format(change,'.2f')).model_dump(mode='json')
                    session.add(ActionExecution(action_id=identifier,user_id=identity.subject_id,ticket_id=ticket.id,event_id=event.id,
                        tool=plan.tool,parameter_hash=request.parameter_hash,draft_hash=request.draft_hash,result=result,result_hash=canonical_hash(result),committed_at=now))
                    request.status='executed';request.updated_at=now
                    await session.flush();await self.checkpoint('before_commit');new_commit=True
        if error:raise error
        if new_commit:await self.checkpoint('after_commit')
        return result


def execution_text(result):
    label='已创建服务申请工单' if result['tool']=='ticket_create' else '已更新工单状态'
    return f"{label}。\n\n工单号：{result['ticket_id']}\n\n状态：{result['ticket_status']}；版本：{result['ticket_version']}。\n\n本次完成的是工单登记或状态流转，未办理实际资金退款。可使用动作 ID 查询本次已提交结果。"


class ExecutionNode:
    def __init__(self,service):self.service=service
    async def __call__(self,state):
        from smart_cs.services.access import principal_context
        plan=state.get('action_plan') or {}
        if not plan or plan.get('read_only') or not state.get('compliance_passed'):return {}
        result=await self.service.execute(principal_context.get(),plan['action_id'],plan['parameter_hash'])
        from smart_cs.services.tasks import completed_execution_plan
        return {'action_execution':result,'task_plan':completed_execution_plan(state.get('task_plan'),result),
            'action_request':{**state.get('action_request',{}),'status':'executed','executed':True,'execution_enabled':True}}
