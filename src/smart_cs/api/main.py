"""
FastAPI入口 — 提供REST API
"""

from __future__ import annotations

import os
import json
from contextlib import asynccontextmanager, aclosing, AsyncExitStack

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from smart_cs.api.dependencies import current_identity, administrator
from smart_cs.api.auth import router as auth_router,allowed_origins
from smart_cs.api.jobs import router as jobs_router
from smart_cs.api.runs import router as runs_router
from smart_cs.api.knowledge_admin import router as knowledge_admin_router
from smart_cs.services.access import document_visibility, can_read_document
from smart_cs.services.orders import OrderService
from smart_cs.repositories.orders import OrderRepository
from smart_cs.schemas.orders import OrderList, RefundEligibility, OrderToolCall, Money
from smart_cs.tools.orders import ORDER_TOOLS, call_order_tool
from smart_cs.schemas.actions import ActionPlan,PlanValidation
from smart_cs.services.actions import ActionPlanService
from smart_cs.schemas.compliance import ReviewedAction
from smart_cs.schemas.approvals import HumanDecision,ActionCancellation
from smart_cs.schemas.execution import ExecutionRequest,ExecutionResult
from smart_cs.tools.business import tool_catalog,READ_TOOLS
from smart_cs.db.models import ChatSession
from smart_cs.schemas.chat import ChatRequest, ChatResponse, ResumeRequest
from smart_cs.schemas.errors import ApplicationError, ErrorResponse
from smart_cs.schemas.observability import RunRecord
from smart_cs.schemas.dependencies import DependencyReport
from smart_cs.schemas.retrieval import RetrievalRequest,RetrievalResponse
from smart_cs.services.dependencies import readiness as dependency_status
from fastapi.responses import JSONResponse
from smart_cs.services.chat import ChatService
from smart_cs.repositories.runs import MemoryRunRepository, DatabaseRunRepository
from smart_cs.api.observability import CorrelationMiddleware, install_error_handlers
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.models import model_credentials_configured
from smart_cs.workflow import workflow_profile
from smart_cs.memory.working_memory import WorkingMemory
from smart_cs.memory.short_term import ShortTermMemory
from smart_cs.memory.long_term import LongTermMemory
from smart_cs.memory.lexical_knowledge import LexicalKnowledgeMemory
from smart_cs.db.database import configured_database
from smart_cs.repositories.tickets import PostgresTicketRepository
from smart_cs.services.tickets import TicketService, AgentTicketReader
from smart_cs.schemas.tickets import TicketRegistration,TicketTransition,TicketAssignment,TicketStatus
from smart_cs.db.models import DatasetRelease, User
from smart_cs.db.models import KnowledgeDocument
from sqlalchemy import select
from smart_cs.tools.mcp_server import MCPToolServer, create_default_tools
from smart_cs.tracing.otel_config import init_tracer, AgentMetrics

from smart_cs.settings import PROJECT_ROOT, ENV_PATH, project_path

FRONTEND_DIST = PROJECT_ROOT / 'frontend' / 'dist'
load_dotenv(ENV_PATH)


working_memory = WorkingMemory()
short_term_memory = ShortTermMemory(redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
long_term_memory = LongTermMemory(index_path=project_path(os.getenv("FAISS_INDEX_PATH", "vector_store/faiss_index")))
mcp_server = create_default_tools(MCPToolServer())
metrics = AgentMetrics()
graph = None
database = None
ticket_store = None
checkpoint_store = None
memory_run_repository = MemoryRunRepository()


def initialize_graph():
    """Keep API docs and tools available while the user configures a model."""
    global graph
    if graph is not None:
        return
    load_dotenv(ENV_PATH, override=True)
    if not model_credentials_configured():
        return
    graph = create_supervisor_graph(
        working_memory=working_memory,
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
        ticket_store=ticket_store,
        order_service=OrderService(OrderRepository(database)) if database is not None else None,
        tool_service=ActionPlanService(OrderService(OrderRepository(database)),TicketService(PostgresTicketRepository(database))) if database is not None else None,
        approval_service=approval_service() if database is not None else None,
        execution_service=execution_service() if database is not None else None,
        checkpointer=checkpoint_store.saver if checkpoint_store is not None else None,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global graph, database, ticket_store, long_term_memory, checkpoint_store

    init_tracer(
        service_name=os.getenv("OTEL_SERVICE_NAME", "smart-cs-multi-agent"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )

    database = configured_database()
    if database is not None:
        await database.initialize(create_schema=os.getenv('DATABASE_AUTO_CREATE', 'false').lower() == 'true')
        from smart_cs.persistence.postgres import PostgresCheckpointStore,MemoryCheckpointLocks
        checkpoint_store=await PostgresCheckpointStore(database).open() if database.engine.dialect.name=='postgresql' else MemoryCheckpointLocks()
        ticket_store = AgentTicketReader(TicketService(PostgresTicketRepository(database)))
        if os.getenv('RAG_ENABLED','false').lower()=='true' and database.engine.dialect.name=='postgresql':
            from smart_cs.memory.bge_memory import BGEKnowledgeMemory
            long_term_memory=BGEKnowledgeMemory(database)
            await long_term_memory.validate_runtime()
        else:
            long_term_memory = LexicalKnowledgeMemory(await database.knowledge())
    else:
        # Explicit offline/test adapter; production .env is configured to PostgreSQL.
        ticket_store = AgentTicketReader(None)
        from smart_cs.datasets import KNOWLEDGE, DISCLAIMER
        for key, _title, body, _qa in KNOWLEDGE:
            long_term_memory.add_document(body + '\n' + DISCLAIMER, source=key + '.md')
    app.state.database = database
    initialize_graph()
    from smart_cs.durable.executor import RunExecutor
    from smart_cs.durable.service import RunService
    app.state.run_service_factory=(lambda:RunService(database,chat_service(graph))) if database is not None and checkpoint_store is not None else None
    app.state.require_workflow=require_graph
    executor=RunExecutor(database,lambda:chat_service(require_graph())) if database is not None and checkpoint_store is not None and os.getenv('RUN_EXECUTOR_ENABLED','true').lower()=='true' else None
    if executor is not None:executor.start()
    try:
        yield
    finally:
        if executor is not None:await executor.close()
        app.state.run_service_factory=None
        if checkpoint_store is not None:await checkpoint_store.close()
        checkpoint_store=None
        graph=None
        if database is not None:
            await database.close()
        database = None
        app.state.database = None
        ticket_store = None


app = FastAPI(
    title="智能客服多Agent系统",
    description="基于LangGraph的Supervisor编排多Agent智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
    responses={status:{'model':ErrorResponse} for status in [400,401,403,404,409,422,429,500,502,503,504]},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins()),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=['X-Request-ID','X-Trace-ID','X-Run-ID'],
)


app.add_middleware(CorrelationMiddleware)
install_error_handlers(app)
app.include_router(auth_router)
app.include_router(jobs_router)
app.include_router(runs_router)
app.include_router(knowledge_admin_router)


def require_graph():
    if database is not None and os.getenv('APP_MODE', 'local_demo') != 'local_demo':
        raise ApplicationError('LOCAL_DEMO_ONLY','当前数据基础版本仅支持本地演示；公开部署前需要完成认证与审批。',503)
    initialize_graph()
    if graph is None:
        raise ApplicationError('MODEL_NOT_CONFIGURED','请先在项目根目录的 .env 填写模型密钥、接口地址和模型名，保存后重新发送请求。',503)

    return graph


def run_repository():
    return DatabaseRunRepository(database) if database is not None else memory_run_repository


def approval_service():
    from smart_cs.services.approvals import ApprovalService
    require_local_database()
    return ApprovalService(database)


def execution_service():
    from smart_cs.services.execution import ActionExecutionService
    require_local_database()
    return ActionExecutionService(database)


def chat_service(workflow):
    from smart_cs.persistence.conversations import ConversationRuntime
    repository=run_repository()
    persistence=ConversationRuntime(database,checkpoint_store,workflow,repository) if database is not None and checkpoint_store is not None else None
    return ChatService(workflow, short_term_memory, repository, metrics,persistence)


async def execution_resources():
    stack=AsyncExitStack()
    try:yield stack
    finally:
        import anyio
        with anyio.CancelScope(shield=True):await stack.aclose()


async def chat_events(request: ChatRequest, workflow, prepared=None):
    service=chat_service(workflow)
    async with aclosing(service.events(request, prepared or service.prepare(request))) as stream:
        async for event in stream:yield event


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, identity=Depends(current_identity),resources=Depends(execution_resources,scope='request')):
    workflow=require_graph()
    if identity.authenticated:
        from smart_cs.durable.service import RunService
        import asyncio
        runs=RunService(database,chat_service(workflow));created=await runs.submit(request,identity)
        while True:
            result=await runs.detail(created['run_id'],identity)
            event=result['result_event']
            if event:
                if event['type']=='error':raise ApplicationError(event['code'],event['message'],event.get('http_status',409))
                return ChatResponse.model_validate(event)
            await asyncio.sleep(.25)
    prepared=await prepare_chat(request,workflow,identity,resources)
    async with aclosing(chat_events(request,workflow,prepared)) as stream:
        async for event in stream:
            if event['type']=='error':
                raise ApplicationError(event['code'],event['message'],event.get('http_status',500))
            if event['type']=='complete':return ChatResponse.model_validate(event)


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, identity=Depends(current_identity),resources=Depends(execution_resources,scope='request')):
    workflow=require_graph();service=chat_service(workflow)
    if identity.authenticated:
        from smart_cs.durable.service import RunService
        runs=RunService(database,service);created=await runs.submit(request,identity)
        return StreamingResponse(runs.subscribe(created['run_id'],identity),media_type='text/event-stream',
            headers={'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Run-ID':created['run_id']})
    prepared=await prepare_chat(request,workflow,identity,resources)
    async def encode():
        async with aclosing(service.events(request,prepared)) as stream:
            async for event in stream:
                yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
    return StreamingResponse(encode(),media_type='text/event-stream',
        headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


async def prepare_chat(request, workflow, identity,resources):
    # Live dependencies only yield authenticated principals. Legacy local identities
    # are supplied exclusively by explicit dependency overrides in offline tests.
    service=chat_service(workflow);prepared=service.prepare(request,identity)
    if identity.authenticated:
        require_local_database()
        if service.conversations is None:raise ApplicationError('CHECKPOINT_UNAVAILABLE','持久化尚未就绪，请先完成数据库准备。',503)
        await service.conversations.prepare(request,prepared,resources)
    return prepared


@app.post('/api/sessions/{session_id}/resume')
async def resume_session(session_id:str,body:ResumeRequest,identity=Depends(current_identity),resources=Depends(execution_resources,scope='request')):
    workflow=require_graph();service=chat_service(workflow)
    if not identity.authenticated or service.conversations is None:raise ApplicationError('RECOVERY_UNAVAILABLE','当前模式不支持恢复。',503)
    from smart_cs.durable.service import RunService
    runs=RunService(database,service)
    created=await runs.submit(ChatRequest(message='resume',session_id=session_id),identity,resume=True,client_request_id=body.client_request_id)
    return StreamingResponse(runs.subscribe(created['run_id'],identity),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Run-ID':created['run_id']})


@app.get('/api/runs/{run_id}', response_model=RunRecord)
async def get_run(run_id: str, identity=Depends(current_identity)):
    record=await run_repository().get(run_id,identity.subject_id)
    if record is None:raise ApplicationError('RUN_NOT_FOUND','当前账号未找到此运行记录。',404)
    if database is not None:
        from smart_cs.db.models import RunControl
        async with database.sessions() as s:control=await s.get(RunControl,run_id)
        if control is not None:
            from smart_cs.durable.service import RunService
            detail=await RunService(database,chat_service(graph)).detail(run_id,identity)
            record={**record,**{k:v for k,v in detail.items() if k!='status'},'run_status':detail['status']}
    return record


@app.get("/api/workflow")
async def get_workflow(identity=Depends(current_identity)):
    initialize_graph()
    return workflow_profile(graph)


@app.get("/api/history/{session_id}")
async def get_history(session_id: str, identity=Depends(current_identity),limit:int=Query(default=100,ge=1,le=200),before:int|None=Query(default=None,ge=1)):
    """获取对话历史"""
    if identity.authenticated:
        require_local_database()
        return await chat_service(graph).conversations.history(session_id,identity,limit,before)
    history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


def require_local_database():
    if os.getenv('APP_MODE', 'local_demo') != 'local_demo':
        raise HTTPException(status_code=503, detail='演示数据接口未开放。')
    if database is None:
        raise HTTPException(status_code=503, detail='请先配置并初始化 PostgreSQL 数据基础。')
    return database


@app.get('/api/foundation')
async def foundation_status(identity=Depends(administrator)):
    db = require_local_database()
    async with db.sessions() as session:
        releases = (await session.scalars(select(DatasetRelease))).all()
        datasets = [{'id': r.id, 'version': r.version, 'provenance': r.provenance,
                     'splits': {path: record['records'] for path, record in r.manifest['files'].items() if path.endswith('.jsonl')},
                     'human_reviewed': False} for r in releases]
    index=await long_term_memory.describe() if hasattr(long_term_memory,'describe') else None
    return {**await db.summary(), 'datasets': datasets, 'retrieval_mode': getattr(long_term_memory, 'retrieval_mode', 'legacy_demo'), 'rag_index':index,
            'identity_mode': 'authenticated_server_session', 'llm_finetuned': False}


@app.get('/api/demo/users')
async def demo_users(identity=Depends(administrator)):
    db = require_local_database()
    async with db.sessions() as session:
        users = (await session.scalars(select(User).where(User.source == 'synthetic_fixture').order_by(User.id).limit(100))).all()
        return {'demo_only': True, 'users': [{'user_id': u.id, 'display_name': u.display_name, 'role': u.role} for u in users]}


@app.get('/api/knowledge/documents')
async def knowledge_documents(identity=Depends(current_identity)):
    db=require_local_database()
    async with db.sessions() as session:
        rows=(await session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.status=='published',document_visibility(identity)).order_by(KnowledgeDocument.source))).all()
        return {'documents':[{'id':d.id,'title':d.title,'source':d.source,'mime_type':d.mime_type,
            'version':d.version,'provenance':d.metadata_json,'file_url':f'/api/knowledge/documents/{d.id}/file'} for d in rows]}


@app.post('/api/knowledge/search',response_model=RetrievalResponse)
async def search_knowledge(body:RetrievalRequest,response:Response,identity=Depends(current_identity)):
    if os.getenv('RAG_ENABLED','false').lower()!='true':raise ApplicationError('RAG_NOT_ENABLED','请先启用真实向量检索并准备索引。',503)
    from smart_cs.retrieval.hybrid import configured_retriever
    response.headers['Cache-Control']='no-store'
    return await configured_retriever(require_local_database(),mode=body.mode).search(body.query,body.top_k,identity=identity,collections=body.collections)


@app.get('/api/knowledge/documents/{document_id}/file')
async def knowledge_file(document_id: str, identity=Depends(current_identity)):
    db=require_local_database()
    async with db.sessions() as session:
        doc=await session.get(KnowledgeDocument,document_id)
        if doc is None or doc.status!='published' or not can_read_document(doc,identity) or not doc.file_path:raise HTTPException(404,'文件未发布或不存在。')
        path=(PROJECT_ROOT/doc.file_path).resolve()
        if not path.is_relative_to((PROJECT_ROOT/'data').resolve()) or path.suffix.lower() not in {'.pdf','.html','.htm','.md','.txt'} or not path.is_file():
            raise HTTPException(404,'文件不在允许的知识目录内。')
        if doc.mime_type=='application/pdf':
            return FileResponse(path,media_type='application/pdf',headers={'Content-Security-Policy':"sandbox",'X-Content-Type-Options':'nosniff'})
        return FileResponse(path,media_type='text/plain; charset=utf-8',filename=doc.source,
            headers={'Content-Security-Policy':"sandbox",'X-Content-Type-Options':'nosniff'})


@app.get('/api/knowledge/documents/{document_id}/chunks/{chunk_id}')
async def knowledge_chunk(document_id:str,chunk_id:str,response:Response,identity=Depends(current_identity)):
    """Authorized historical block locator; current retrieval eligibility is explicit."""
    from smart_cs.db.models import KnowledgeChunk
    from smart_cs.retrieval.policy import current_document_ids,business_date
    db=require_local_database();response.headers['Cache-Control']='no-store'
    async with db.sessions() as session:
        row=(await session.execute(select(KnowledgeChunk,KnowledgeDocument).join(
            KnowledgeDocument,KnowledgeDocument.id==KnowledgeChunk.document_id).where(
            KnowledgeChunk.id==chunk_id,KnowledgeDocument.id==document_id,
            KnowledgeDocument.status=='published',document_visibility(identity)))).first()
        if row is None:raise HTTPException(404,'切片未发布或不存在。')
        chunk,document=row
        import hashlib
        if hashlib.sha256(chunk.content.encode()).hexdigest()!=chunk.content_hash:
            raise ApplicationError('CHUNK_CONTENT_CHANGED','切片内容校验失败。',409)
        current=await session.scalar(select(KnowledgeDocument.id).where(
            KnowledgeDocument.id==document_id,KnowledgeDocument.id.in_(current_document_ids(identity,business_date()))))
        return {'document_id':document_id,'chunk_id':chunk.id,'version':document.version,
            'source':document.source,'content':chunk.content,'chunk_sha256':chunk.content_hash,
            'artifact_sha256':document.content_hash,'page':chunk.source_page,'heading':chunk.heading,
            'locator':chunk.locator,'currently_effective':bool(current),'historical_locator':True,
            'effective_from':document.effective_from,'effective_until':document.effective_until,
            'file_url':f'/api/knowledge/documents/{document_id}/file'+(f'#page={chunk.source_page}' if chunk.source_page else '')}


def order_service():
    return OrderService(OrderRepository(require_local_database()))


@app.get('/api/orders',response_model=OrderList)
async def list_orders(identity=Depends(current_identity), limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    return await order_service().list(identity,limit,offset)


@app.get('/api/orders/{order_id}')
async def get_order(order_id: str,identity=Depends(current_identity)):
    return {'demo_only':True,'order':(await order_service().get(identity,order_id)).model_dump(mode='json')}


@app.get('/api/orders/{order_id}/refund-eligibility',response_model=RefundEligibility)
async def refund_eligibility(order_id: str,requested_amount: Money | None=None,identity=Depends(current_identity)):
    return await order_service().eligibility(identity,order_id,requested_amount)


def ticket_service():
    return TicketService(PostgresTicketRepository(require_local_database()))


@app.get('/api/tickets')
async def list_tickets(identity=Depends(current_identity),limit:int=Query(default=50,ge=1,le=100),offset:int=Query(default=0,ge=0),status:TicketStatus|None=None):
    return await ticket_service().list(identity,limit,offset,status=status)


@app.post('/api/tickets',status_code=201)
async def register_ticket(body:TicketRegistration,identity=Depends(current_identity)):
    row=await ticket_service().register(identity,body)
    return {'registration_only':True,'ticket':row.model_dump(mode='json')}


@app.get('/api/tickets/{ticket_id}')
async def get_ticket(ticket_id:str,identity=Depends(current_identity)):
    return {'demo_only':True,'ticket':(await ticket_service().get(identity,ticket_id)).model_dump(mode='json')}


@app.get('/api/tickets/{ticket_id}/events')
async def ticket_events(ticket_id:str,identity=Depends(current_identity),limit:int=Query(default=50,ge=1,le=100),offset:int=Query(default=0,ge=0)):
    return await ticket_service().events(identity,ticket_id,limit,offset)


@app.post('/api/tickets/{ticket_id}/assignment')
async def assign_ticket(ticket_id:str,body:TicketAssignment,identity=Depends(current_identity)):
    return {'ticket':(await ticket_service().assign(identity,ticket_id,body)).model_dump(mode='json')}


@app.post('/api/tickets/{ticket_id}/transitions')
async def transition_ticket(ticket_id:str,body:TicketTransition,identity=Depends(current_identity)):
    return {'ticket':(await ticket_service().transition(identity,ticket_id,body)).model_dump(mode='json')}


@app.get('/api/support/staff')
async def support_staff(identity=Depends(current_identity)):
    return {'staff':await ticket_service().repository.staff()}


@app.get('/api/support/tickets')
async def support_tickets(identity=Depends(current_identity),limit:int=Query(default=50,ge=1,le=100),offset:int=Query(default=0,ge=0),status:TicketStatus|None=None):
    return await ticket_service().list(identity,limit,offset,assigned=True,status=status)


@app.get('/api/sessions')
async def list_sessions(identity=Depends(current_identity),limit:int=Query(default=50,ge=1,le=100),offset:int=Query(default=0,ge=0)):
    db=require_local_database()
    async with db.sessions() as session:
        rows=(await session.scalars(select(ChatSession).where(ChatSession.user_id==identity.subject_id).order_by(ChatSession.updated_at.desc(),ChatSession.id).limit(limit+1).offset(offset))).all()
        return {'sessions':[{'id':r.id,'title':r.title,'status':r.status,'graph_version':r.graph_version,'updated_at':r.updated_at.isoformat()} for r in rows[:limit]],
            'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None,'persistence':'database'}


@app.get('/api/sessions/{session_id}')
async def session_detail(session_id:str,identity=Depends(current_identity)):
    require_local_database()
    return await chat_service(graph).conversations.detail(session_id,identity)


@app.get('/api/tools')
async def list_tools(identity=Depends(current_identity)):
    return {'tools':tool_catalog(protected_execution=database is not None)}


@app.post('/api/tools/call')
async def call_tool(request: OrderToolCall, identity=Depends(current_identity)):
    if request.name not in READ_TOOLS:
        raise ApplicationError('TOOL_FORBIDDEN','此工具未开放直接调用，请通过受控业务流程处理。',403)
    arguments=dict(request.arguments)
    # Legacy read adapter only: this deprecated field never selects an owner.
    # ActionPlan and all other tools reject it rather than accepting authority.
    if request.name=='order_query':arguments.pop('user_id',None)
    service=ActionPlanService(order_service(),ticket_service())
    return {'success':True,'result':await service.call_read(identity,request.name,arguments),'error':None}


@app.post('/api/actions/validate',response_model=PlanValidation)
async def validate_action(body:ActionPlan,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await ActionPlanService(order_service(),ticket_service()).validate(identity,body)


@app.get('/api/action-requests')
async def list_action_requests(response:Response,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return {'items':await approval_service().list(identity,limit=limit,offset=offset)}


@app.get('/api/action-requests/{action_id}')
async def get_action_request(action_id:str,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await approval_service().get(identity,action_id)


@app.post('/api/action-requests/{action_id}/confirm')
async def confirm_action_request(action_id:str,body:HumanDecision,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await approval_service().decide(identity,action_id,body)


@app.post('/api/action-requests/{action_id}/cancel')
async def cancel_action_request(action_id:str,body:ActionCancellation,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await approval_service().cancel(identity,action_id,body)


@app.get('/api/action-requests/{action_id}/execution',response_model=ExecutionResult)
async def action_execution_result(action_id:str,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await execution_service().get(identity,action_id)


@app.post('/api/action-requests/{action_id}/execute',response_model=ExecutionResult)
async def execute_action_request(action_id:str,body:ExecutionRequest,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await execution_service().execute(identity,action_id,body.parameter_hash)


@app.get('/api/staff/action-requests')
async def staff_action_requests(response:Response,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return {'items':await approval_service().list(identity,staff=True,limit=limit,offset=offset)}


@app.get('/api/staff/action-requests/{action_id}')
async def staff_action_request(action_id:str,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await approval_service().get(identity,action_id,staff=True)


@app.post('/api/staff/action-requests/{action_id}/decide')
async def decide_action_request(action_id:str,body:HumanDecision,response:Response,identity=Depends(current_identity)):
    response.headers['Cache-Control']='no-store'
    return await approval_service().decide(identity,action_id,body,staff=True)


@app.post('/api/actions/review',response_model=ReviewedAction)
async def review_action(body:ActionPlan,response:Response,identity=Depends(current_identity)):
    from smart_cs.models import build_agent_models
    from smart_cs.services.compliance import ComplianceReviewService
    response.headers['Cache-Control']='no-store'
    actions=ActionPlanService(order_service(),ticket_service())
    plan=await actions.validate(identity,body)
    review=await ComplianceReviewService(build_agent_models()['compliance_checker'],actions).review_action(identity,plan.model_dump(mode='json'))
    return ReviewedAction(plan=plan if review.decision in {'allow','require_approval'} else None,
        validation_status=plan.status,missing_fields=plan.missing_fields,review=review)


@app.get("/api/metrics")
async def get_metrics(identity=Depends(administrator)):
    """获取系统指标"""
    return {
        "agent_metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy" if graph is not None else "configuration_required",
        "version": "1.0.0",
        "chat_ready": graph is not None,
        "scope": "api_configuration_only",
        "upstream_model_health": "not_probed",
    }


@app.get('/health/live')
async def liveness():
    return {'status':'alive','scope':'api_process_only'}


@app.get('/health/ready',response_model=DependencyReport,responses={503:{'model':DependencyReport}})
async def dependency_readiness():
    report=await dependency_status()
    return JSONResponse(report.model_dump(),status_code=200 if report.ready else 503)


@app.get('/api/dependencies',response_model=DependencyReport)
async def dependency_details(identity=Depends(administrator)):
    if os.getenv('APP_MODE','local_demo')!='local_demo':
        raise ApplicationError('LOCAL_DEMO_ONLY','依赖详情仅在本地演示模式开放。',503)
    return await dependency_status()


@app.get('/api/admin/performance')
async def performance_dashboard(identity=Depends(administrator)):
    from smart_cs.performance.dashboard import snapshot
    return await snapshot(require_local_database())



@app.get("/", include_in_schema=False)
async def chat_page():
    """Serve the built React application from the same origin as the API."""
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail="聊天界面尚未构建，请先完成前端构建。")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    icon = FRONTEND_DIST / "favicon.svg"
    if not icon.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(icon, media_type="image/svg+xml")


# Only the built assets directory is public. Project configuration and source
# files, including .env, are never exposed through this mount.
app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets", check_dir=False), name="frontend-assets")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "smart_cs.api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
