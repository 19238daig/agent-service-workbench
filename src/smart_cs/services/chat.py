import asyncio
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4
from langchain_core.messages import HumanMessage
from smart_cs.schemas.chat import ChatResponse
from smart_cs.schemas.identity import IdentityContext
from smart_cs.services.identity import local_identity
from smart_cs.schemas.errors import classify_failure
from smart_cs.observability.context import RunObservation,correlation,run_context,usage_summary
from smart_cs.workflow import NODE_IDS,ROLES,node_summary,workflow_profile


@dataclass
class PreparedRun:
    identity: IdentityContext
    session_id: str
    ids: object
    run_id: str
    persistent: bool=False
    thread_id: str=''
    turn_id: str=''
    user_message_id: str=''
    message: str=''
    kind: str='new'
    resumed_from_run_id: str|None=None
    execution_saver: object=None
    record: dict|None=None
    context_hash: str=''
    durable: bool=False
    attempt_token: str=''
    terminal_event: dict|None=None


class ChatService:
    def __init__(self,workflow,memory,repository,metrics,conversations=None):
        self.workflow=workflow;self.memory=memory;self.repository=repository;self.metrics=metrics
        self.conversations=conversations

    def prepare(self,request,identity=None,session_id=None):
        ids=correlation();ids.run_id=uuid4().hex
        return PreparedRun(identity or local_identity(request.user_id),session_id or request.session_id or str(uuid4()),ids,ids.run_id)

    async def events(self,request,prepared):
        started=perf_counter();trace=[];starts={};open_nodes={}
        session_id=prepared.session_id;ids=prepared.ids
        observation=RunObservation(prepared.run_id,ids.request_id,ids.trace_id)
        if prepared.durable:
            from smart_cs.durable.store import EventStore
            observation.sink=EventStore(self.conversations.database).model_call
        token=run_context.set(observation)
        from smart_cs.services.access import principal_context
        principal_token=principal_context.set(prepared.identity)
        from smart_cs.persistence.context import active_saver,execution_binding,GRAPH_VERSION
        saver_token=active_saver.set(prepared.execution_saver)
        binding={'identity_context':prepared.identity.model_dump(exclude={'auth_session_id'}),'user_id':prepared.identity.subject_id,
            'session_id':session_id,'run_id':prepared.run_id,'request_id':ids.request_id,'trace_id':ids.trace_id,'turn_id':prepared.turn_id,
            'business_context_fingerprint':prepared.context_hash}
        binding_token=execution_binding.set(binding)
        record={'run_id':prepared.run_id,'request_id':ids.request_id,'trace_id':ids.trace_id,
            'session_id':session_id,'identity':prepared.identity.model_dump(),'status':'running',
            'recording_backend':self.repository.backend,'nodes':[],'duration_ms':None,'error_code':None,
            'usage':usage_summary([])}
        if prepared.record:record=prepared.record
        registered=prepared.persistent;finished=False
        def envelope(event):return {**event,'request_id':ids.request_id,'trace_id':ids.trace_id,'run_id':prepared.run_id}
        async def finish(status,error_code=None,response=None):
            nonlocal finished
            from smart_cs.performance.runtime import summary as performance_summary
            record.update(status=status,error_code=error_code,duration_ms=round((perf_counter()-started)*1000,3),nodes=trace,usage=usage_summary(observation.calls),performance=performance_summary(observation))
            record['performance']['first_execution_progress_ms']=min((max(0,s.get('elapsed_ms',0)-s.get('duration_ms',0)) for s in trace if 'elapsed_ms' in s),default=None)
            if prepared.persistent:await self.conversations.finish(prepared,record,observation.calls,response)
            else:await self.repository.finish(record,observation.calls)
            finished=True
        try:
            try:
                if not prepared.persistent:await self.repository.start(record)
                registered=True
            except Exception:
                yield envelope({'type':'error','code':'RUN_RECORDING_UNAVAILABLE','message':'调用记录存储不可用，本轮尚未开始执行。','http_status':503});return
            yield envelope({'type':'session','session_id':session_id,'recording_status':self.repository.backend,
                'turn_id':prepared.turn_id,'persistence':self.conversations.store.backend if prepared.persistent else ''})
            if not prepared.persistent:await self.memory.add_message(session_id,'user',request.message)
            state={'messages':[HumanMessage(content=prepared.message or request.message,id=prepared.user_message_id or None)],**binding,
                'intent':'','intent_details':{},'agent_steps':[],'agent_models':{},'knowledge_evidence':{},'ticket_evidence':{},'action_plan':{},'action_review':{},'content_review':{},
                'task_proposal':{},'task_result':{},'action_request':{},'action_execution':{},'sub_results':{},'compliance_passed':True,'final_response':'','current_agent':'','retry_count':0}
            models={node.get('role'):node['model'] for node in workflow_profile(self.workflow)['nodes'] if node.get('role')}
            config={'configurable':{'thread_id':prepared.thread_id or session_id},'metadata':{'request_id':ids.request_id,'run_id':prepared.run_id,'trace_id':ids.trace_id}}
            result=None;waiting_approval=False
            try:
                if prepared.persistent:state,result=await self.conversations.graph_input(prepared,state,config)
                async def graph_events():
                    if result is not None:return
                    async for item in self.workflow.astream_events(state,config=config,version='v2',durability='sync'):yield item
                async for event in graph_events():
                    name,kind=event.get('name'),event['event']
                    if name in NODE_IDS and kind=='on_chain_start':
                        starts[event['run_id']]=perf_counter();open_nodes[event['run_id']]=name
                        yield envelope({'type':'node_start','node':name,'model':models.get(ROLES[name],''),'elapsed_ms':round((perf_counter()-started)*1000,1)})
                    elif name in NODE_IDS and kind=='on_chain_end':
                        output=event.get('data',{}).get('output',{})
                        if not isinstance(output,dict):continue
                        duration=round((perf_counter()-starts.get(event['run_id'],started))*1000,1)
                        self.metrics.record_call(name,duration,True);open_nodes.pop(event['run_id'],None)
                        step={'node':name,'model':output.get('agent_models',{}).get(ROLES[name],''),'duration_ms':duration,
                            'elapsed_ms':round((perf_counter()-started)*1000,1),
                            'status':'blocked' if (name in {'compliance_check','execution_review'} and not output.get('compliance_passed')) or (
                                name=='action_review' and output.get('action_review',{}).get('decision')=='block') else 'done',
                            'summary':node_summary(name,output)}
                        trace.append(step);yield envelope({'type':'node_end',**step})
                    elif kind=='on_chain_end' and not event.get('parent_ids'):result=event.get('data',{}).get('output')
                if prepared.persistent and 'approval_gate' in self.workflow.get_graph().nodes:
                    snapshot=await self.workflow.aget_state(config)
                    if any(task.interrupts for task in snapshot.tasks):
                        waiting_approval=True
                        result=dict(snapshot.values)
                        from smart_cs.services.approvals import ApprovalService
                        pending=await ApprovalService(self.conversations.database).for_turn(prepared.identity,prepared.turn_id)
                        if not pending:raise RuntimeError('Missing durable approval request')
                        result['action_request']=pending
                if not isinstance(result,dict) or not result.get('final_response'):raise RuntimeError('Missing workflow result')
                if prepared.persistent and (getattr(self.workflow,'config',None) or {}).get('metadata',{}).get('protected_execution_protocol')=='action-execution-v1':
                    from smart_cs.services.execution import ActionExecutionService
                    from smart_cs.schemas.errors import ApplicationError
                    committed=await ActionExecutionService(self.conversations.database).for_turn(prepared.identity,prepared.turn_id)
                    if (result.get('action_execution') or None)!=committed:
                        raise ApplicationError('EXECUTION_BINDING_CHANGED','请查询已提交的动作结果，本轮回复尚未完成。',409)
                if (getattr(self.workflow,'config',None) or {}).get('metadata',{}).get('review_protocol')=='dual-review-v1' and result.get('compliance_passed'):
                    from smart_cs.services.compliance import release_allowed,candidate_text
                    if not release_allowed(result) or result['final_response']!=candidate_text(result):
                        from smart_cs.schemas.errors import ApplicationError
                        raise ApplicationError('CONTENT_REVIEW_BINDING_CHANGED','待发内容或审查绑定已变化，本轮未发布。',409)
                    plan=result.get('action_plan')
                    if plan and not plan.get('read_only') and self.conversations is not None and not result.get('action_execution'):
                        from smart_cs.schemas.actions import PlanValidation
                        from smart_cs.services.actions import ActionPlanService
                        from smart_cs.services.orders import OrderService
                        from smart_cs.services.tickets import TicketService
                        from smart_cs.repositories.orders import OrderRepository
                        from smart_cs.repositories.tickets import PostgresTicketRepository
                        from smart_cs.services.compliance import ComplianceReviewService
                        db=self.conversations.database
                        actions=ActionPlanService(OrderService(OrderRepository(db)),TicketService(PostgresTicketRepository(db)))
                        await ComplianceReviewService(None,actions).refresh(prepared.identity,PlanValidation.model_validate(plan))
                        if result.get('action_request',{}).get('status')=='ready':
                            from smart_cs.services.approvals import ApprovalService
                            await ApprovalService(db).resume_check(prepared.identity,result['action_request']['action_id'])
                evidence=result.get('knowledge_evidence',{})
                if result.get('compliance_passed') and evidence.get('protocol')=='grounded-rag-v1' and evidence.get('citations'):
                    # Review may take time; validate again just before release,
                    # including replay of a completed private checkpoint.
                    from smart_cs.retrieval.dense import DenseRetriever
                    if self.conversations is None:raise RuntimeError('Verified citations require the database runtime')
                    await DenseRetriever(self.conversations.database).revalidate_citations(evidence['citations'],prepared.identity)
            except Exception as error:
                code,status,message=classify_failure(error)
                for key,name in open_nodes.items():
                    duration=round((perf_counter()-starts[key])*1000,1);self.metrics.record_call(name,duration,False)
                    trace.append({'node':name,'status':'error','duration_ms':duration,'error_code':code})
                committed={}
                if prepared.persistent:
                    from smart_cs.services.execution import ActionExecutionService
                    try:committed=await ActionExecutionService(self.conversations.database).for_turn(prepared.identity,prepared.turn_id) or {}
                    except Exception:pass
                prepared.terminal_event=envelope({'type':'error','code':code,'error_type':type(error).__name__,'message':message,'http_status':status,'action_execution':committed})
                await finish('failed',code)
                yield {**prepared.terminal_event,'duration_ms':record['duration_ms']};return
            from smart_cs.schemas.tasks import public_plan
            response=ChatResponse(response=result['final_response'],session_id=session_id,intent=result.get('intent','unknown'),
                compliance_passed=result.get('compliance_passed',True),classification=result.get('intent_details',{}),
                agent_steps=result.get('agent_steps',[]),agent_models=result.get('agent_models',{}),execution_trace=trace,
                duration_ms=round((perf_counter()-started)*1000,1),
                citations=result.get('knowledge_evidence',{}).get('citations',[]) if result.get('compliance_passed') else [],
                knowledge_result={k:result.get('knowledge_evidence',{}).get(k) for k in ['status','reason_code','support_check','protocol']}
                    if result.get('compliance_passed') and result.get('knowledge_evidence') else {},
                action_plan=result.get('action_plan',{}) if result.get('compliance_passed') else {},
                action_review=result.get('action_review',{}),content_review=result.get('content_review',{}),
                action_request=result.get('action_request',{}) if result.get('compliance_passed') or result.get('action_execution') else {},
                action_execution=result.get('action_execution',{}),
                task_plan=public_plan(result.get('task_plan')),
                request_id=ids.request_id,trace_id=ids.trace_id,run_id=prepared.run_id,recording_status=self.repository.backend,
                turn_id=prepared.turn_id,graph_version=GRAPH_VERSION if prepared.persistent else '',
                persistence=self.conversations.store.backend if prepared.persistent else '')
            if not prepared.persistent:await self.memory.add_message(session_id,'assistant',response.response)
            await finish('waiting_approval' if waiting_approval else 'succeeded' if response.compliance_passed else 'blocked',response=response)
            yield {'type':'complete',**response.model_dump()}
        except (asyncio.CancelledError,GeneratorExit):
            if registered and not finished:
                try:
                    import anyio
                    with anyio.CancelScope(shield=True):
                        await asyncio.shield(finish('cancelled','RUN_CANCELLED'))
                except Exception:pass
            raise
        except Exception:
            # A logging/storage failure never replays a graph or claims a business rollback.
            if registered and not finished:
                try:await finish('failed','RUN_RECORDING_FAILED')
                except Exception:pass
            yield envelope({'type':'error','code':'RUN_RECORDING_FAILED','message':'运行或记录保存未完成，请先核实已有业务结果，避免重复提交。','http_status':503})
        finally:
            execution_binding.reset(binding_token)
            active_saver.reset(saver_token)
            principal_context.reset(principal_token)
            run_context.reset(token)
