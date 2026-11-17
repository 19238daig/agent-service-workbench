"""Durable human approval, independent of model output and business execution.

This service has no business write port. Every decision binds the immutable
reviewed draft, requester, facts and expiry. A ready request is still unexecuted.
"""
from datetime import timedelta, timezone
from uuid import uuid4
from sqlalchemy import select
from smart_cs.db.models import ActionRequest, Approval, ChatTurn, User, AuthCredential, utcnow
from smart_cs.schemas.actions import PlanValidation
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.compliance import release_allowed, ComplianceReviewService, canonical_hash, candidate_text

SNAPSHOT_KEYS = ('action_plan','action_review','content_review','sub_results','knowledge_evidence',
                 'compliance_passed','final_response','session_id','turn_id')
TERMINAL = {'rejected','cancelled','expired','stale','executed'}
POLICY_VERSION = 'approval-v1-request-900s-consent-300s-user-then-distinct-staff'


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def failure(code, message='动作确认状态无效，请刷新草稿或新建对话。', status=409):
    return ApplicationError(code, message, status)


class ApprovalService:
    def __init__(self, database, actions=None, clock=utcnow):
        self.database=database; self.clock=clock
        if actions is None:
            from smart_cs.services.actions import ActionPlanService
            from smart_cs.services.orders import OrderService
            from smart_cs.services.tickets import TicketService
            from smart_cs.repositories.orders import OrderRepository
            from smart_cs.repositories.tickets import PostgresTicketRepository
            actions=ActionPlanService(OrderService(OrderRepository(database)), TicketService(PostgresTicketRepository(database)))
        self.actions=actions

    def authorize(self, row, identity, staff=False):
        if not identity or not identity.authenticated:
            raise failure('AUTHENTICATION_REQUIRED', '请先登录。', 401)
        if row is None: raise failure('ACTION_NOT_FOUND','当前账号未找到此动作。',404)
        if staff:
            if identity.role not in {'support','admin'}: raise failure('STAFF_REQUIRED','此操作需要人工客服权限。',403)
            if row.user_id==identity.subject_id: raise failure('SELF_APPROVAL_FORBIDDEN','人工审批必须由另一位客服完成。',403)
            if row.required_approval!='staff' or row.status=='awaiting_user': raise failure('ACTION_NOT_FOUND','当前账号未找到此动作。',404)
        elif row.user_id!=identity.subject_id:
            raise failure('ACTION_NOT_FOUND','当前账号未找到此动作。',404)

    async def load(self, session, identifier):
        return await session.scalar(select(ActionRequest).where(ActionRequest.id==identifier).with_for_update())

    def view(self, row, approvals=()):
        status=row.status
        if status not in TERMINAL and aware(row.expires_at)<=self.clock(): status='expired'
        if status not in TERMINAL and any(aware(a.expires_at)<=self.clock() for a in approvals): status='expired'
        return {'action_id':row.id,'session_id':row.session_id,'turn_id':row.turn_id,
            'tool':row.tool,'parameters':row.snapshot['action_plan']['parameters'] if canonical_hash(row.snapshot)==row.draft_hash and release_allowed(row.snapshot) else {},
            'parameter_hash':row.parameter_hash,'draft_hash':row.draft_hash,
            'status':status,'required_approval':row.required_approval,'expires_at':aware(row.expires_at).isoformat(),
            'created_at':aware(row.created_at).isoformat(), 'execution_enabled':row.status=='executed','executed':row.status=='executed',
            'execution_url':'/api/action-requests/'+row.id+'/execution',
            'approvals':[{'stage':a.stage,'decision':a.decision,'principal_id':a.principal_id,
                'principal_role':a.principal_role,'expires_at':aware(a.expires_at).isoformat(),
                'created_at':aware(a.created_at).isoformat()} for a in approvals]}

    async def records(self, session, row):
        return list((await session.scalars(select(Approval).where(Approval.action_id==row.id).order_by(Approval.created_at))).all())

    async def task_freshness(self,session,row,identity):
        raw=row.snapshot.get('task_plan')
        if not raw:return
        from smart_cs.schemas.tasks import TaskPlan
        from smart_cs.db.models import KnowledgeDocument,RetrievalIndex
        from smart_cs.persistence.conversations import context_fingerprint
        from smart_cs.retrieval.dense import DenseRetriever
        plan=TaskPlan.model_validate(raw)
        waiting=[t for t in plan.tasks if t.status=='waiting_approval']
        completed={t.id for t in plan.tasks if t.status=='completed'}
        if (plan.actor_id!=row.user_id or plan.session_id!=row.session_id or plan.status!='waiting_approval'
            or len(waiting)!=1 or waiting[0].kind!=row.tool or not set(waiting[0].depends_on)<=completed
            or plan.missing_fields or waiting[0].result.get('validation',{}).get('parameter_hash')!=row.parameter_hash):raise ValueError()
        citations=[c for t in plan.tasks if t.kind=='policy_lookup' for c in t.result.get('knowledge_evidence',{}).get('citations',[])]
        ids=sorted({c['document_id'] for c in citations if 'document_id' in c})
        if ids:
            await session.execute(select(KnowledgeDocument.id).where(KnowledgeDocument.id.in_(ids)).order_by(KnowledgeDocument.id).with_for_update(read=True))
            await session.execute(select(RetrievalIndex.id).where(RetrievalIndex.active.is_(True)).order_by(RetrievalIndex.id).with_for_update(read=True))
        if plan.context_hash!=await context_fingerprint(session,identity):raise ValueError()
        if citations:await DenseRetriever(self.database).revalidate_citations(citations,identity)

    async def freshness(self, session, row, *, approvals=False):
        """Return errors so expiry/staleness can commit before the caller raises."""
        if row.status in TERMINAL: return failure('ACTION_'+row.status.upper())
        if aware(row.expires_at)<=self.clock():
            row.status='expired'; return failure('ACTION_EXPIRED')
        try:
            if canonical_hash(row.snapshot)!=row.draft_hash or not release_allowed(row.snapshot): raise ValueError()
            plan=PlanValidation.model_validate(row.snapshot['action_plan'])
            if (plan.action_id!=row.id or plan.actor_id!=row.user_id or plan.parameter_hash!=row.parameter_hash
                or plan.tool!=row.tool or row.required_approval!=row.snapshot['action_review']['required_approval']): raise ValueError()
            user=await session.get(User,row.user_id)
            credential=await session.get(AuthCredential,row.user_id)
            if not user or user.role!=row.actor_role or (credential and not credential.enabled): raise ValueError()
            identity=IdentityContext(subject_id=row.user_id, role=user.role, authenticated=True, mode='authenticated_session')
            await self.task_freshness(session,row,identity)
            await ComplianceReviewService(None,self.actions).refresh(identity,plan)
            for decision in await self.records(session,row):
                principal=await session.get(User,decision.principal_id)
                cred=await session.get(AuthCredential,decision.principal_id)
                if (decision.parameter_hash!=row.parameter_hash or decision.draft_hash!=row.draft_hash
                    or not principal or principal.role!=decision.principal_role or (cred and not cred.enabled)
                    or (decision.stage=='user' and decision.principal_id!=row.user_id)
                    or (decision.stage=='staff' and (principal.role not in {'support','admin'} or principal.id==row.user_id))): raise ValueError()
                if aware(decision.expires_at)<=self.clock():
                    row.status='expired'; return failure('APPROVAL_EXPIRED')
            if approvals:
                records={a.stage:a for a in await self.records(session,row)}
                required={'user','staff'} if row.required_approval=='staff' else {'user'}
                if row.status not in {'approved','ready'} or not required.issubset(records) or any(records[k].decision!='approve' for k in required):
                    return failure('APPROVAL_REQUIRED','请先完成用户确认及必要的人工审批。')
        except (ValueError,TypeError,KeyError,ApplicationError):
            row.status='stale'; return failure('ACTION_STALE')
        return None

    async def create(self, identity, state):
        if not release_allowed(state) or not state.get('compliance_passed') or state.get('final_response')!=candidate_text(state): raise failure('UNREVIEWED_DRAFT')
        plan=PlanValidation.model_validate(state['action_plan'])
        if plan.read_only or plan.actor_id!=identity.subject_id or state['action_review']['decision']!='require_approval': raise failure('INVALID_APPROVAL_DRAFT')
        snapshot={k:state.get(k,{}) for k in SNAPSHOT_KEYS}
        if state.get('task_plan'):snapshot['task_plan']=state['task_plan']
        digest=canonical_hash(snapshot)
        async with self.database.sessions.begin() as session:
            # A durable turn lock serializes gate replay, even across processes.
            turn=await session.scalar(select(ChatTurn).where(ChatTurn.id==state.get('turn_id'),ChatTurn.user_id==identity.subject_id).with_for_update())
            if (not turn or turn.session_id!=state.get('session_id') or turn.status!='running'
                or turn.current_run_id!=state.get('run_id')): raise failure('ACTION_TURN_MISMATCH')
            row=await session.scalar(select(ActionRequest).where(ActionRequest.turn_id==turn.id).with_for_update())
            if row:
                self.authorize(row,identity)
                if row.id!=plan.action_id or row.draft_hash!=digest: raise failure('ACTION_BINDING_MISMATCH')
            else:
                now=self.clock()
                row=ActionRequest(id=plan.action_id,session_id=turn.session_id,user_id=identity.subject_id,turn_id=turn.id,
                    actor_role=identity.role,tool=plan.tool,parameter_hash=plan.parameter_hash,draft_hash=digest,snapshot=snapshot,
                    required_approval=state['action_review']['required_approval'],status='awaiting_user',
                    expires_at=now+timedelta(minutes=15),created_at=now,updated_at=now)
                session.add(row); await session.flush()
            error=None if row.status=='executed' else await self.freshness(session,row)
            view=self.view(row,await self.records(session,row))
        if error: raise error
        return view

    async def get(self, identity, identifier, *, staff=False):
        async with self.database.sessions.begin() as session:
            row=await self.load(session,identifier); self.authorize(row,identity,staff)
            await self.freshness(session,row)
            return self.view(row,await self.records(session,row))

    async def list(self, identity, *, staff=False, limit=50, offset=0):
        if not identity.authenticated: raise failure('AUTHENTICATION_REQUIRED',status=401)
        if staff and identity.role not in {'support','admin'}: raise failure('STAFF_REQUIRED',status=403)
        async with self.database.sessions() as session:
            query=select(ActionRequest)
            if staff: query=query.where(ActionRequest.required_approval=='staff', ActionRequest.status=='awaiting_staff',
                ActionRequest.user_id!=identity.subject_id, ActionRequest.expires_at>self.clock(),
                select(Approval.id).where(Approval.action_id==ActionRequest.id,Approval.stage=='user',
                    Approval.decision=='approve',Approval.expires_at>self.clock()).exists())
            else: query=query.where(ActionRequest.user_id==identity.subject_id)
            rows=(await session.scalars(query.order_by(ActionRequest.created_at.desc()).offset(offset).limit(limit))).all()
            # Queue contains only displayable drafts; freshness is enforced again on decision.
            return [self.view(r,await self.records(session,r)) for r in rows if canonical_hash(r.snapshot)==r.draft_hash and release_allowed(r.snapshot)]

    async def decide(self, identity, identifier, body, *, staff=False):
        async with self.database.sessions.begin() as session:
            row=await self.load(session,identifier); self.authorize(row,identity,staff)
            principal=await session.get(User,identity.subject_id)
            if not principal or principal.role!=identity.role: raise failure('AUTHORIZATION_CHANGED',status=403)
            if body.parameter_hash!=row.parameter_hash: raise failure('ACTION_BINDING_MISMATCH')
            error=await self.freshness(session,row)
            stage='staff' if staff else 'user'
            existing=next((a for a in await self.records(session,row) if a.stage==stage),None)
            if not error and existing:
                if existing.principal_id!=identity.subject_id or existing.decision!=body.decision: error=failure('APPROVAL_CONFLICT')
            elif not error:
                expected='awaiting_staff' if staff else 'awaiting_user'
                if row.status!=expected: error=failure('APPROVAL_STAGE_MISMATCH')
                else:
                    now=self.clock()
                    session.add(Approval(id=uuid4().hex,action_id=row.id,stage=stage,principal_id=identity.subject_id,
                        principal_role=identity.role,decision=body.decision,parameter_hash=row.parameter_hash,draft_hash=row.draft_hash,
                        note=body.note,created_at=now,expires_at=min(aware(row.expires_at),now+timedelta(minutes=5))))
                    row.status='rejected' if body.decision=='reject' else ('awaiting_staff' if not staff and row.required_approval=='staff' else 'approved')
                    row.updated_at=now; await session.flush()
            view=self.view(row,await self.records(session,row))
        if error: raise error
        return view

    async def cancel(self, identity, identifier, body):
        async with self.database.sessions.begin() as session:
            row=await self.load(session,identifier); self.authorize(row,identity)
            if body.parameter_hash!=row.parameter_hash: raise failure('ACTION_BINDING_MISMATCH')
            if row.status in {'ready','executed'}: raise failure('ACTION_ALREADY_RESUMED')
            if row.status not in TERMINAL: row.status='cancelled'; row.updated_at=self.clock()
            return self.view(row,await self.records(session,row))

    async def resume_check(self, identity, identifier, *, mark_ready=False):
        async with self.database.sessions.begin() as session:
            row=await self.load(session,identifier); self.authorize(row,identity)
            if row.status=='executed':
                from smart_cs.services.execution import ActionExecutionService
                await ActionExecutionService(self.database).get(identity,identifier)
                return self.view(row,await self.records(session,row))
            error=await self.freshness(session,row,approvals=True)
            if not error and mark_ready: row.status='ready'; row.updated_at=self.clock()
            view=self.view(row,await self.records(session,row))
        if error: raise error
        return view

    async def for_turn(self, identity, turn_id):
        async with self.database.sessions() as session:
            row=await session.scalar(select(ActionRequest).where(ActionRequest.turn_id==turn_id,ActionRequest.user_id==identity.subject_id))
            return self.view(row,await self.records(session,row)) if row else None


class ApprovalGate:
    def __init__(self,service): self.service=service

    async def __call__(self,state):
        from langgraph.types import interrupt
        from smart_cs.services.access import principal_context
        from smart_cs.persistence.context import execution_binding
        state={**state,**(execution_binding.get() or {})}
        plan=state.get('action_plan') or {}
        if not plan or plan.get('read_only') or not state.get('compliance_passed'): return {}
        identity=principal_context.get()
        view=await self.service.create(identity,state)
        if view['status']=='executed':return {'action_request':view}
        # Node replay is intentional. create is idempotent; there are no business effects.
        value=interrupt({'type':'action_approval','action_id':view['action_id'],'parameter_hash':view['parameter_hash']})
        if value!={'action_id':view['action_id']}: raise failure('INVALID_RESUME_COMMAND')
        view=await self.service.resume_check(identity,view['action_id'],mark_ready=True)
        return {'action_request':view}
