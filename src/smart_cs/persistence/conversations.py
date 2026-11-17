"""Canonical product history and fenced, owner-scoped read-only recovery.

Checkpoint contents are private infrastructure data. Only reviewed final answers
are published here, in the same transaction as the turn and run completion.
"""
import hashlib
import json
from uuid import uuid4
from sqlalchemy import select
from smart_cs.db.models import ChatSession,ChatMessage,ChatTurn,ExecutionRun,KnowledgeDocument,RetrievalIndex,Ticket,Order,utcnow
from smart_cs.persistence.context import GRAPH_VERSION
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.access import document_visibility
from smart_cs.services.sessions import owned_session


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def metadata(workflow):return (getattr(workflow,'config',None) or {}).get('metadata',{})


def model_fingerprint(workflow):
    return metadata(workflow).get('workflow_model_fingerprint') or digest(metadata(workflow).get('workflow_models',{}))


async def context_fingerprint(session,identity):
    from smart_cs.retrieval.policy import business_date
    from smart_cs.retrieval.hybrid import pipeline_identity
    from smart_cs.retrieval.reranking import pipeline_identity as rerank_identity
    from smart_cs.performance.policy import identity as serving_identity
    from smart_cs.retrieval.grounding import policy_fingerprint
    from smart_cs.schemas.actions import contract_fingerprint
    from smart_cs.services.compliance import policy_fingerprint as compliance_fingerprint
    from smart_cs.services.approvals import POLICY_VERSION as approval_policy
    from smart_cs.schemas.tasks import PROTOCOL as task_protocol
    docs=(await session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.status=='published',document_visibility(identity)).order_by(KnowledgeDocument.id))).all()
    indexes=(await session.scalars(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))).all()
    # Conservatively refuse replay after a visible business fact or assignment changes.
    scope=Ticket.user_id==identity.subject_id if identity.role=='customer' else Ticket.assignee_id==identity.subject_id
    tickets=(await session.execute(select(Ticket.id,Ticket.version,Ticket.assignee_id).where(scope).order_by(Ticket.id))).all()
    orders=(await session.execute(select(Order.id,Order.fact_version).where(Order.user_id==identity.subject_id).order_by(Order.id))).all()
    return digest({'task_protocol':task_protocol,'approval_policy':approval_policy,'compliance_policy':compliance_fingerprint(),'action_contract':contract_fingerprint(),'rag_policy':policy_fingerprint(),'as_of':business_date().isoformat(),'documents':[(d.id,d.content_hash,d.version,d.effective_from,d.effective_until,d.access_scope,d.owner_id,d.metadata_json) for d in docs],
        'indexes':[(i.id,i.model_identity,i.configuration) for i in indexes], 'tickets':[tuple(r) for r in tickets],'orders':[tuple(r) for r in orders],
        'rerank_pipeline':rerank_identity(),'serving_policy':serving_identity(),**({'retrieval_pipeline':pipeline_identity()} if pipeline_identity() else {})})


class ConversationRuntime:
    def __init__(self,database,store,workflow,repository):
        self.database=database;self.store=store;self.workflow=workflow;self.repository=repository

    async def latest(self,session,identifier):
        return await session.scalar(select(ChatTurn).where(ChatTurn.session_id==identifier).order_by(ChatTurn.created_at.desc(),ChatTurn.id.desc()).limit(1))

    async def compatibility(self,session,row,turn,identity):
        if row.status!='active':return 'SESSION_ARCHIVED'
        if row.graph_version!=GRAPH_VERSION or not row.thread_id:return 'GRAPH_VERSION_INCOMPATIBLE'
        if metadata(self.workflow).get('graph_version')!=GRAPH_VERSION:return 'GRAPH_VERSION_INCOMPATIBLE'
        if turn is not None:
            protected=metadata(self.workflow).get('protected_execution_protocol')=='action-execution-v1'
            if metadata(self.workflow).get('agent_writes_enabled',True) and not protected:return 'RECOVERY_WRITES_DISABLED_REQUIRED'
            if turn.graph_version!=GRAPH_VERSION:return 'GRAPH_VERSION_INCOMPATIBLE'
            if turn.authorization_role!=identity.role:return 'RECOVERY_AUTHORIZATION_CHANGED'
            if protected:
                from smart_cs.services.execution import ActionExecutionService
                # The business commit itself changes the old context fingerprint.
                # Recovery of its deterministic tail must still be possible.
                committed=await ActionExecutionService(self.database).for_turn(identity,turn.id)
                if committed:return None
            if turn.model_fingerprint!=model_fingerprint(self.workflow):return 'RECOVERY_MODELS_CHANGED'
            if turn.context_fingerprint!=await context_fingerprint(session,identity):return 'RECOVERY_CONTEXT_CHANGED'
        return None

    async def prepare(self,request,prepared,resources,resume=False,enqueue=None):
        identity=prepared.identity
        row=await owned_session(self.database,prepared.session_id,identity) if request.session_id or resume else None
        thread_id=row.thread_id if row else 'cs-'+uuid4().hex
        if row and (not thread_id or row.graph_version!=GRAPH_VERSION):
            raise ApplicationError('GRAPH_VERSION_INCOMPATIBLE','此会话使用旧版流程，请新建对话；原有历史仍可查看。',409)
        # Lifetime belongs to the request, including the entire StreamingResponse.
        prepared.execution_saver=await resources.enter_async_context(self.store.lock(thread_id))
        prepared.persistent=True;prepared.thread_id=thread_id
        async with self.database.sessions.begin() as session:
            if row:
                row=await session.scalar(select(ChatSession).where(ChatSession.id==row.id,ChatSession.user_id==identity.subject_id).with_for_update())
            else:
                row=ChatSession(id=prepared.session_id,user_id=identity.subject_id,title=request.message[:200],
                    thread_id=thread_id,graph_version=GRAPH_VERSION,source='authenticated_chat')
                session.add(row);await session.flush()
            turn=await self.latest(session,row.id)
            unfinished=turn is not None and turn.status not in {'completed','blocked'}
            reason=await self.compatibility(session,row,turn if resume else None,identity)
            if reason:raise ApplicationError(reason,'当前流程或授权上下文已变化，请新建对话；历史已保留。',409)
            if resume:
                from smart_cs.db.models import RunControl
                active=await session.get(RunControl,turn.current_run_id) if turn else None
                if active and active.status in {'queued','running','cancel_requested'}:
                    raise ApplicationError('RUN_STILL_ACTIVE','本轮仍在后台处理，请重新订阅事件，不要重复恢复。',409)
                if not unfinished:raise ApplicationError('TURN_ALREADY_COMPLETED','此轮已处理完成，请刷新历史。',409)
                from smart_cs.services.approvals import ApprovalService
                approvals=ApprovalService(self.database)
                pending=await approvals.for_turn(identity,turn.id)
                if pending: await approvals.resume_check(identity,pending['action_id'])
                old_run=await session.get(ExecutionRun,turn.current_run_id)
                if old_run.status=='running':
                    old_run.status='failed';old_run.finished_at=utcnow()
                    old_run.snapshot={**old_run.snapshot,'status':'failed','error_code':'EXECUTION_INTERRUPTED'}
                prepared.resumed_from_run_id=turn.current_run_id
                prepared.turn_id=turn.id;prepared.user_message_id=turn.user_message_id;prepared.kind='resume'
                message=await session.get(ChatMessage,turn.user_message_id)
                prepared.message=message.content
            else:
                if unfinished:raise ApplicationError('TURN_RECOVERY_REQUIRED','上轮尚未完成，请恢复上轮处理，或新建对话。',409)
                prepared.turn_id=uuid4().hex;prepared.user_message_id=uuid4().hex;prepared.message=request.message
            prepared.context_hash=turn.context_fingerprint if resume else await context_fingerprint(session,identity)
            prepared.record=self.record(prepared)
            await self.repository.start_in_session(session,prepared.record)
            if resume:
                turn.current_run_id=prepared.run_id;turn.attempt_count+=1;turn.status='running';turn.assistant_message_id=None;turn.error_code=None;turn.updated_at=utcnow()
            else:
                session.add(ChatMessage(id=prepared.user_message_id,session_id=row.id,user_id=identity.subject_id,
                    sequence=row.next_sequence,role='user',content=request.message,run_id=prepared.run_id,source='authenticated_chat'))
                row.next_sequence+=1
                await session.flush()
                session.add(ChatTurn(id=prepared.turn_id,session_id=row.id,user_id=identity.subject_id,
                    current_run_id=prepared.run_id,user_message_id=prepared.user_message_id,graph_version=GRAPH_VERSION,
                    authorization_role=identity.role,context_fingerprint=await context_fingerprint(session,identity),
                    model_fingerprint=model_fingerprint(self.workflow),status='running'))
            row.active_run_id=prepared.run_id;row.updated_at=utcnow();row.version+=1
            await session.flush()
            if enqueue is not None:await enqueue(session,prepared)
        return prepared

    def record(self,p):
        from smart_cs.observability.context import usage_summary
        return {'run_id':p.run_id,'request_id':p.ids.request_id,'trace_id':p.ids.trace_id,'session_id':p.session_id,
            'identity':p.identity.model_dump(exclude={'auth_session_id'}),'status':'queued' if p.durable else 'running','recording_backend':self.repository.backend,
            'nodes':[],'duration_ms':None,'error_code':None,'usage':usage_summary([]),'turn_id':p.turn_id,
            'graph_version':GRAPH_VERSION,'execution_kind':p.kind,'resumed_from_run_id':p.resumed_from_run_id}

    async def graph_input(self,p,state,config):
        if p.kind!='resume':return state,None
        snapshot=await self.workflow.aget_state(config)
        if metadata(self.workflow).get('protected_execution_protocol')=='action-execution-v1':
            from smart_cs.services.execution import ActionExecutionService
            committed=await ActionExecutionService(self.database).for_turn(p.identity,p.turn_id)
            if committed and (not snapshot or not snapshot.values or snapshot.values.get('turn_id')!=p.turn_id
                or not set(snapshot.next)<= {'approval_gate','execute_action','execution_review','execution_synthesize'}):
                raise ApplicationError('CHECKPOINT_EXECUTION_MISMATCH','业务已提交，请查询动作结果；此检查点不能自动重放。',409)
        if not snapshot or not snapshot.values:return state,None
        # Crash before this turn's input checkpoint can leave the previous
        # completed turn as the thread's latest state. Never publish its answer
        # as the answer to a new user message.
        if snapshot.values.get('turn_id')!=p.turn_id:
            if snapshot.next:raise ApplicationError('CHECKPOINT_TURN_MISMATCH','检查点所属轮次不一致，请新建对话。',409)
            return state,None
        if not snapshot.next:
            if not snapshot.values.get('final_response'):raise ApplicationError('CHECKPOINT_INCOMPATIBLE','检查点缺少完整结果，请新建对话。',409)
            return None,snapshot.values
        if any(task.interrupts for task in snapshot.tasks):
            from smart_cs.services.approvals import ApprovalService
            from langgraph.types import Command
            approvals=ApprovalService(self.database)
            pending=await approvals.for_turn(p.identity,p.turn_id)
            if not pending:raise ApplicationError('APPROVAL_REQUIRED','缺少持久化动作确认记录。',409)
            await approvals.resume_check(p.identity,pending['action_id'])
            return Command(resume={'action_id':pending['action_id']}),None
        return None,None

    async def finish(self,p,record,calls,response=None):
        async with self.database.sessions.begin() as session:
            if p.durable:
                from smart_cs.durable.store import terminal
                response=await terminal(session,p,record,response)
            row=await session.scalar(select(ChatSession).where(ChatSession.id==p.session_id,ChatSession.user_id==p.identity.subject_id).with_for_update())
            turn=await session.get(ChatTurn,p.turn_id)
            if row.active_run_id!=p.run_id or turn.current_run_id!=p.run_id:
                raise ApplicationError('RUN_SUPERSEDED','此轮已由其他请求接管，请刷新历史。',409)
            await self.repository.finish_in_session(session,record,calls)
            if response:
                identifier=uuid4().hex
                # An allowlist of public response metadata; never checkpoint.values.
                safe=response.model_dump(exclude={'response','session_id','persistence'})
                session.add(ChatMessage(id=identifier,session_id=row.id,user_id=row.user_id,sequence=row.next_sequence,
                    role='assistant',content=response.response,run_id=p.run_id,metadata_json=safe,source='reviewed_final'))
                row.next_sequence+=1
                await session.flush()
                turn.assistant_message_id=identifier
                turn.status='waiting_approval' if record['status']=='waiting_approval' else 'completed' if response.compliance_passed else 'blocked'
            else:turn.status=record['status'] if record['status'] in {'cancelled','interrupted'} else 'failed'
            turn.error_code=record.get('error_code');turn.updated_at=utcnow()
            row.active_run_id=None;row.updated_at=utcnow();row.version+=1

    async def detail(self,identifier,identity):
        row=await owned_session(self.database,identifier,identity)
        async with self.database.sessions() as session:
            turn=await self.latest(session,identifier)
            unfinished=turn is not None and turn.status not in {'completed','blocked'}
            reason=await self.compatibility(session,row,turn if unfinished else None,identity)
            final=await session.get(ChatMessage,turn.assistant_message_id) if turn and turn.assistant_message_id else None
            tasks=(final.metadata_json or {}).get('task_plan',{}) if final else {}
            from smart_cs.db.models import RunControl
            control=await session.get(RunControl,turn.current_run_id) if turn else None
        from smart_cs.services.approvals import ApprovalService
        pending=await ApprovalService(self.database).for_turn(identity,turn.id) if turn else None
        if unfinished and pending and not reason:
            try: await ApprovalService(self.database).resume_check(identity,pending['action_id'])
            except ApplicationError as error: reason=error.code
        active=control is not None and control.status in {'queued','running','cancel_requested'}
        return {'session_id':identifier,'title':row.title,'status':row.status,'graph_version':row.graph_version,
            'persistence':'postgresql' if self.store.backend=='postgresql' else self.store.backend,
            'can_continue':not reason and not unfinished,'can_resume':unfinished and not reason and not active,
            'recovery_reason':reason,'turn_status':turn.status if turn else None,'turn_id':turn.id if turn else None,
            'action_request':pending or {},'task_plan':tasks,'active_run_id':control.run_id if active else None,
            'run_status':control.status if control else None,'last_run_id':turn.current_run_id if turn else None}

    async def history(self,identifier,identity,limit=100,before=None):
        detail=await self.detail(identifier,identity)
        async with self.database.sessions() as session:
            query=select(ChatMessage).where(ChatMessage.session_id==identifier,ChatMessage.user_id==identity.subject_id)
            if before is not None:query=query.where(ChatMessage.sequence<before)
            rows=list((await session.scalars(query.order_by(ChatMessage.sequence.desc()).limit(limit+1))).all())
        more=len(rows)>limit;rows=list(reversed(rows[:limit]))
        return {**detail,'messages':[{'id':m.id,'sequence':m.sequence,'role':m.role,'content':m.content,
            'run_id':m.run_id,'metadata':m.metadata_json or {},'created_at':m.created_at.isoformat()} for m in rows],
            'has_more':more,'before_sequence':rows[0].sequence if rows and more else None}
