"""Bounded deterministic scheduling; the router proposes tasks, never authority."""
import copy
import re
from uuid import uuid4
from decimal import Decimal
from langchain_core.messages import HumanMessage
from smart_cs.schemas.tasks import TaskPlan,Task,Slots,PROTOCOL
from smart_cs.schemas.actions import ActionPlan
from smart_cs.schemas.orders import OrderFacts,RefundEligibility
from smart_cs.agents.action_planner import has_literal,explicit_amounts,plan_turn
from smart_cs.agents.context import latest_user_turn,append_step,record_model
from smart_cs.services.actions import format_plan
from smart_cs.services.orders import format_order
from smart_cs.services.access import principal_context

WRITES={'ticket_create','ticket_transition'}
PRIORITY={'policy_lookup':0,'order_query':1,'order_eligibility':2,'ticket_query':3,'ticket_create':4,'ticket_transition':5}
SLOT_LABELS={'order_id':'本人订单号','ticket_id':'工单号','requested_amount':'本次申请金额（元）','type':'申请类型（退款、投诉或通用服务）',
    'summary':'申请摘要','details':'具体原因或说明','target_status':'希望变更的状态','expected_version':'工单当前版本','reason':'变更原因'}
REFUND_POLICY_QUERY='请依据知识库解释七日无理由退货的适用条件和例外，保留资料来源、条件和适用范围；仅解释政策。'


def policy_query(goal):
    if '自动续费' in goal:return '请依据知识库说明自动续费日期前的消费者提醒要求，保留条件、来源和适用范围。'
    if re.search(r'物流|配送|发货',goal):return '请依据知识库解释配送、发货及物流相关规定，保留来源、条件和适用范围。'
    if re.search(r'退款|退货|七日',goal):return REFUND_POLICY_QUERY
    return '仅解释下列请求涉及的资料政策，不办理业务；保留来源与适用范围：'+goal


def wants_write(text):
    if re.search(r'(?:不要|不需要|不想|无需|不再).{0,8}(?:创建|申请|登记|修改)|只查|仅查询|只咨询|仅咨询',text):return False
    return bool(re.search(r'(?:帮我|为我|替我|请|我要|我想|并|然后|再|同时).{0,30}(?:申请|登记|创建|提交|办理|关闭|流转|修改)',text))


def cancelled(text):return bool(re.search(r'算了|取消.{0,8}(?:申请|办理|任务)|不要继续|不再申请|先别申请',text))


def sanitized_slots(raw,goal,replies,previous=None):
    proposed=Slots.model_validate(raw);slots=(previous or Slots()).model_copy(deep=True)
    text='\n'.join([goal,*replies]);amounts=explicit_amounts([HumanMessage(content=t) for t in [goal,*replies]])
    for key,value in proposed.model_dump(exclude_none=True).items():
        if key in {'order_id','ticket_id'} and not has_literal(value,text):continue
        if key=='requested_amount' and Decimal(value) not in amounts:continue
        if key=='expected_version' and not re.search(r'(?:版本|version)[^\d]{0,15}'+str(value)+r'(?!\d)',text,re.I):continue
        setattr(slots,key,value)
    if slots.type is None:
        if re.search(r'退款|退货',goal):slots.type='refund'
        elif '投诉' in goal:slots.type='complaint'
    if slots.priority is None:slots.priority='medium'
    if slots.details and not slots.summary:slots.summary='退款申请' if slots.type=='refund' else '服务申请'
    return slots


def stop(plan,reason):
    plan.status='blocked';plan.active_task_id=None
    for task in plan.tasks:
        if task.status not in {'completed','blocked'}:task.status='blocked';task.reason_code=reason


def missing(task,slots):
    keys={'policy_lookup':[], 'order_query':['order_id'],'order_eligibility':['order_id'],'ticket_query':['ticket_id'],
        'ticket_create':['type','summary','details'],'ticket_transition':['ticket_id','target_status','expected_version','reason']}[task.kind]
    if task.kind=='ticket_create' and slots.type=='refund':keys=keys+['order_id','requested_amount']
    return [k for k in keys if getattr(slots,k) is None]


class TaskOrchestrator:
    def __init__(self,tools,ticket_model):self.tools=tools;self.ticket_model=ticket_model

    async def initialize(self,state):
        query,_=latest_user_turn(state.get('messages',[]));old=state.get('task_plan') or {}
        proposal=state.get('task_proposal') or {};kinds=proposal.get('tasks') or []
        pending=bool(old and old.get('status')=='needs_information')
        readonly=bool(re.search(r'只查|仅查询|只咨询|仅咨询|(?:不要|不需要).{0,8}(?:创建|登记|申请|修改)',query))
        if pending and readonly and kinds and not any(k in WRITES for k in kinds):pending=False
        if not kinds and not pending:return {'task_plan':{}}
        if not pending and state.get('intent_details',{}).get('confidence',0)<0.5:return {'task_plan':{}}
        identity=principal_context.get()
        if identity is None or not identity.authenticated:return {'task_plan':{}}
        results={};evidence={};notice=''
        if pending:
            plan=TaskPlan.model_validate(old)
            if plan.actor_id!=identity.subject_id or plan.session_id!=state['session_id']:
                return {'task_plan':{}}
            if cancelled(query) or readonly:
                stop(plan,'USER_CANCELLED');return {'task_plan':plan.model_dump(),'sub_results':{'task_notice':'已停止这份待补信息的任务计划，尚未提交新的业务操作。'},'action_plan':{}}
            if len(plan.replies)>=6 or plan.steps_used>=plan.max_steps:
                stop(plan,'TASK_BUDGET_EXCEEDED')
                return {'task_plan':plan.model_dump(),'sub_results':{'task_notice':'这份计划已达到处理步数上限，请新建对话明确需求。'},'action_plan':{}}
            plan.replies.append(query);plan.clarification_rounds+=1;plan.steps_used+=1
            plan.slots=sanitized_slots(proposal.get('slots',{}),plan.goal,plan.replies,plan.slots)
            changed=plan.context_hash!=state.get('business_context_fingerprint','')
            plan.context_hash=state.get('business_context_fingerprint','')
            for task in plan.tasks:
                # Recheck time-sensitive/owned business facts on every continuation.
                if changed or task.kind!='policy_lookup' or task.status!='completed':
                    task.status='pending';task.result={};task.reason_code=''
            plan.status='running';plan.missing_fields=[];plan.active_task_id=None
            if changed:notice='相关事实或资料上下文已变化，本轮会重新核验前置任务。'
        else:
            if not kinds:return {'task_plan':{}}
            slots=sanitized_slots(proposal.get('slots',{}),query,[])
            kinds=list(dict.fromkeys(kinds))
            if any(k in WRITES for k in kinds) and not wants_write(query):kinds=[k for k in kinds if k not in WRITES]
            if not kinds:return {'task_plan':{}}
            if len([k for k in kinds if k in WRITES])>1:
                kinds=kinds[:6];bad=True
            else:bad=False
            if 'ticket_create' in kinds and slots.type=='refund' and 'order_eligibility' not in kinds:kinds.append('order_eligibility')
            if 'ticket_transition' in kinds and 'ticket_query' not in kinds:kinds.append('ticket_query')
            kinds=sorted(kinds,key=lambda k:PRIORITY[k])
            tasks=[Task(id=f't{i+1}',kind=k,depends_on=[f't{i}'] if i else []) for i,k in enumerate(kinds[:6])]
            plan=TaskPlan(id='tp-'+uuid4().hex,actor_id=identity.subject_id,session_id=state['session_id'],
                context_hash=state.get('business_context_fingerprint',''),goal=query,slots=slots,tasks=tasks)
            if bad or len(kinds)>6:stop(plan,'TASK_SCOPE_TOO_LARGE');notice='本轮最多处理一项需要确认的写动作，请分开提出多项办理请求。'
        for task in plan.tasks:
            if task.status=='completed':
                results[task.id]=task.result.get('text','')
                if task.kind=='policy_lookup':evidence=task.result.get('knowledge_evidence',{})
        if notice:results['task_notice']=notice
        return {'task_plan':plan.model_dump(),'sub_results':results,'knowledge_evidence':evidence,
            'action_plan':{},'agent_steps':append_step(state,'task_initialize')}

    async def dispatch(self,state):
        raw=state.get('task_plan') or {}
        if not raw:return {}
        plan=TaskPlan.model_validate(raw);results=dict(state.get('sub_results',{}))
        if plan.status=='running':
            completed={t.id for t in plan.tasks if t.status=='completed'}
            task=next((t for t in plan.tasks if t.status=='pending' and set(t.depends_on)<=completed),None)
            if task is None:
                if all(t.status=='completed' for t in plan.tasks):plan.status='completed'
                else:stop(plan,'TASK_DEPENDENCY_UNSATISFIED')
            elif plan.steps_used>=plan.max_steps:
                stop(plan,'TASK_BUDGET_EXCEEDED');results['task_notice']='本轮已达到处理步数上限，请新建对话继续。'
            else:
                fields=missing(task,plan.slots)
                if fields:
                    task.status='needs_information';plan.status='needs_information';plan.missing_fields=fields
                    results['task_notice']='前面的核验已保留。请补充：'+'、'.join(SLOT_LABELS[k] for k in fields)+'；补齐后仍需确认和必要审批，当前不会创建或修改工单。'
                else:task.status='running';plan.active_task_id=task.id;plan.steps_used+=1
        return {'task_plan':plan.model_dump(),'sub_results':results,'agent_steps':append_step(state,'task_dispatch')}

    @staticmethod
    def route(state):
        raw=state.get('task_plan') or {}
        if not raw or raw['status']!='running' or not raw.get('active_task_id'):return 'finish'
        task=next(t for t in raw['tasks'] if t['id']==raw['active_task_id'])
        return 'knowledge_rag' if task['kind']=='policy_lookup' else 'ticket_handler'

    async def business(self,state):
        raw=state.get('task_plan') or {}
        if not raw:return None
        plan=TaskPlan.model_validate(raw);task=next(t for t in plan.tasks if t.id==plan.active_task_id);slots=plan.slots
        identity=principal_context.get();data=None;reason='';validation={}
        if task.kind in WRITES:
            args=({k:v for k,v in slots.model_dump().items() if k in {'type','order_id','requested_amount','priority','summary','details'}} if task.kind=='ticket_create'
                else {k:v for k,v in slots.model_dump().items() if k in {'ticket_id','target_status','expected_version','reason'}})
            expected=ActionPlan(action={'name':task.kind,'arguments':args})
            # The original human goal/replies remain the provenance for identifiers and money.
            focused='请只为这一项已明确请求的任务提出草稿，不执行。参数必须原样复制，包括summary/details/reason，不得改写：'+expected.model_dump_json()
            history=[HumanMessage(content=s) for s in [plan.goal,*plan.replies]]
            planning={**state,'intent_details':{**state.get('intent_details',{}),'secondary':task.kind,'suggested_agent':'ticket_handler'}}
            validation,info,text=await plan_turn(self.ticket_model,self.tools,planning,focused,history)
            if not validation or validation['tool']!=task.kind or validation['parameters']!=expected.action.arguments.model_dump(mode='json'):
                validation={};reason='TASK_ACTION_MISMATCH';text='动作计划与已收集的信息不一致，本轮未继续办理。'
        else:
            args={'order_id':slots.order_id} if task.kind in {'order_query','order_eligibility'} else {'ticket_id':slots.ticket_id}
            if task.kind=='order_eligibility':args['requested_amount']=slots.requested_amount
            assessed,data=await self.tools.assess(identity,ActionPlan(action={'name':task.kind,'arguments':args}),binding=state.get('turn_id'))
            assessed.executed=assessed.status=='read_ready';validation=assessed.model_dump(mode='json')
            if assessed.status!='read_ready':text=format_plan(assessed)
            elif task.kind=='order_query':text=format_order(OrderFacts.model_validate(data[0]))
            elif task.kind=='order_eligibility':
                checked=RefundEligibility.model_validate(data);text=format_order(checked.facts,checked)
            else:text='工单号：'+data['ticket_id']+'；状态：'+data['status']+'；版本：'+str(data['version'])+'。'
        facts=dict(state.get('ticket_evidence') or {})
        if data is not None:
            visible=data if task.kind!='ticket_query' else {k:data[k] for k in ['ticket_id','status','version','allowed_transitions']}
            facts[task.id]={'tool':task.kind,'result':visible}
        return {**state,'action_plan':validation,'ticket_evidence':facts,'task_result':{'data':data,'reason_code':reason,'validation':validation,'text':text},
            'current_agent':'ticket_handler','agent_steps':append_step(state,'ticket_handler'),
            'agent_models':record_model(state,'ticket_handler',self.ticket_model)}

    async def collect(self,state):
        if not state.get('task_plan'):return {}
        plan=TaskPlan.model_validate(state['task_plan']);task=next(t for t in plan.tasks if t.id==plan.active_task_id)
        results=dict(state.get('sub_results',{}));action=state.get('action_plan') or {}
        if task.kind=='policy_lookup':
            evidence=state.get('knowledge_evidence',{});text=results.pop('knowledge_rag','')
            task.result={'text':text,'knowledge_evidence':evidence}
            okay=evidence.get('status')=='answered' and evidence.get('protocol')=='grounded-rag-v1' and bool(evidence.get('citations'))
            if not okay:stop(plan,'POLICY_EVIDENCE_UNAVAILABLE')
            else:task.status='completed'
        else:
            task.result=state.get('task_result') or {};text=task.result.get('text','')
            if not action or action.get('status') in {'rejected','needs_information'}:stop(plan,'TASK_VALIDATION_FAILED')
            elif task.kind=='order_eligibility' and task.result['data']['decision']!='allowed':
                task.status='completed';stop(plan,'ORDER_NOT_ELIGIBLE');results['task_notice']='订单核验未满足自动登记条件，后续办理已停止，请核实申请条件。'
            elif task.kind=='ticket_query':
                task.status='completed'
                if plan.slots.expected_version is None:plan.slots.expected_version=task.result['data']['version']
            elif task.kind in WRITES:task.status='waiting_approval';plan.status='waiting_approval'
            else:task.status='completed'
            if task.kind in WRITES and plan.status=='blocked':results['task_notice']='动作计划未通过检查，未继续办理，请核实信息或重新提出请求。'
        if task.kind not in WRITES:results[task.id]=text
        plan.active_task_id=None
        return {'task_plan':plan.model_dump(),'sub_results':results,'task_result':{},'agent_steps':append_step(state,'task_collect')}

    async def after_review(self,state):
        if not state.get('task_plan'):return {}
        plan=TaskPlan.model_validate(state['task_plan'])
        if not state.get('compliance_passed') or (plan.status=='waiting_approval' and not state.get('action_plan')):
            stop(plan,'CONTENT_OR_ACTION_REVIEW_BLOCKED')
        return {'task_plan':plan.model_dump()}


def completed_execution_plan(raw,result):
    if not raw:return {}
    plan=TaskPlan.model_validate(raw)
    for task in plan.tasks:
        if task.status=='waiting_approval':task.status='completed';task.result={'action_id':result['action_id'],'ticket_id':result['ticket_id']}
    plan.status='completed';plan.active_task_id=None;plan.missing_fields=[]
    return plan.model_dump()
