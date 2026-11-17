"""Bounded live acceptance; one labelled interrupted turn, no business writes.

prepare -> restart API -> recover (real models) -> restart -> verify-restart.
Use --followup with verify-restart only to explicitly exercise one paid turn.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import httpx
from dotenv import load_dotenv
from sqlalchemy import select
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import ChatSession,ChatMessage
from smart_cs.persistence.postgres import PostgresCheckpointStore
from smart_cs.persistence.conversations import ConversationRuntime
from smart_cs.repositories.runs import DatabaseRunRepository
from smart_cs.schemas.chat import ChatRequest
from smart_cs.schemas.identity import IdentityContext
from smart_cs.services.chat import ChatService
from smart_cs.services.tickets import AgentTicketReader,TicketService
from smart_cs.repositories.tickets import PostgresTicketRepository
from smart_cs.services.orders import OrderService
from smart_cs.repositories.orders import OrderRepository
from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.memory.bge_memory import BGEKnowledgeMemory
from smart_cs.tracing.otel_config import AgentMetrics
from authenticated_client import authenticated_client
from verify_core_schema import fingerprints


async def business():
    db=configured_database()
    try:
        snapshot=await fingerprints(db)
        return {name:snapshot[name] for name in ['orders','tickets','ticket_events']}
    finally:await db.close()


def history_digest(history):
    value=[{key:m[key] for key in ['id','sequence','role','content','run_id']} for m in history['messages']]
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


async def prepare_interrupted(owner):
    db=configured_database();store=await PostgresCheckpointStore(db).open()
    try:
        graph=create_supervisor_graph(ticket_store=AgentTicketReader(TicketService(PostgresTicketRepository(db))),
            order_service=OrderService(OrderRepository(db)),long_term_memory=BGEKnowledgeMemory(db),checkpointer=store.saver)
        repo=DatabaseRunRepository(db);runtime=ConversationRuntime(db,store,graph,repo)
        service=ChatService(graph,None,repo,AgentMetrics(),runtime)
        identity=IdentityContext(subject_id=owner['id'],role=owner['role'],authenticated=True,mode='authenticated_session')
        request=ChatRequest(message='【第十步中断恢复验收】请只查询工单 TK-20261114-9A0001 的处理状态，不要创建或修改工单。')
        p=service.prepare(request,identity)
        async with AsyncExitStack() as stack:await runtime.prepare(request,p,stack)
        return {'session_id':p.session_id,'thread_id':p.thread_id,'interrupted_run_id':p.run_id,'turn_id':p.turn_id}
    finally:await store.close();await db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','recover','verify-restart']);parser.add_argument('--followup',action='store_true')
    args=parser.parse_args();load_dotenv(ENV_PATH)
    folder=PROJECT_ROOT/'.local/conversations';folder.mkdir(parents=True,exist_ok=True);state_path=folder/'live-case.json'
    report={'passed':False,'real_api':True,'real_models':args.action=='recover' or args.followup,'action':args.action}
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=240) as client,authenticated_client(client,'customer-a') as owner:
            if args.action=='prepare':
                if state_path.exists():raise RuntimeError('Acceptance case already exists; refusing duplicate creation')
                state={'business_before':asyncio.run(business()),**asyncio.run(prepare_interrupted(owner))}
                state_path.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
                detail=client.get('/api/sessions/'+state['session_id']).json()
                assert detail['can_resume'] and detail['turn_status']=='running'
                report.update(passed=True,prepared_without_model_execution=True,session_id=state['session_id'])
            else:
                state=json.loads(state_path.read_text(encoding='utf-8'));identifier=state['session_id']
                if args.action=='recover':
                    response=client.post('/api/sessions/'+identifier+'/resume',json={});assert response.status_code==200
                    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                    final=next(e for e in events if e['type']=='complete')
                    assert final['session_id']==identifier and final['turn_id']==state['turn_id']
                    assert final['compliance_passed'] and 'TK-20261114-9A0001' in final['response']
                    assert 'ticket_handler' in [e['node'] for e in final['execution_trace']]
                    history=client.get('/api/history/'+identifier).json()
                    assert [m['sequence'] for m in history['messages']]==[1,2]
                    assert history['can_continue'] and not history['can_resume']
                    assert history['messages'][0]['run_id']==state['interrupted_run_id']
                    old=client.get('/api/runs/'+state['interrupted_run_id']).json()
                    new=client.get('/api/runs/'+final['run_id']).json()
                    assert old['error_code']=='EXECUTION_INTERRUPTED' and old['status']=='failed'
                    assert new['resumed_from_run_id']==old['run_id'] and new['status']=='succeeded'
                    assert all(call['run_id']==new['run_id'] for call in new['model_calls'])
                    with httpx.Client(base_url=str(client.base_url),trust_env=False) as foreign,authenticated_client(foreign,'customer-b'):
                        assert foreign.get('/api/history/'+identifier).status_code==404
                        assert foreign.post('/api/sessions/'+identifier+'/resume',json={}).status_code==404
                        assert foreign.get('/api/runs/'+final['run_id']).status_code==404
                    assert client.post('/api/sessions/'+identifier+'/resume',json={'thread_id':state['thread_id']}).status_code==422
                    assert client.post('/api/sessions/'+identifier+'/resume',json={}).status_code==409
                    state.update(history_digest=history_digest(history),recovered_run_id=final['run_id'],message_count=2)
                    report.update(real_model_calls=len(new['model_calls']),nodes=[e['node'] for e in final['execution_trace']],
                        interrupted_run_preserved=True,no_duplicate_user_message=True,foreign_access_denied=True)
                else:
                    history=client.get('/api/history/'+identifier).json()
                    assert history_digest(history)==state['history_digest'] and len(history['messages'])==state['message_count']
                    report['restart_history_preserved']=True
                    if args.followup:
                        response=client.post('/api/chat',json={'session_id':identifier,'message':'刚才那张工单的当前状态是什么？只查询，不办理。'})
                        assert response.status_code==200
                        final=response.json();assert final['compliance_passed'] and 'TK-20261114-9A0001' in final['response']
                        history=client.get('/api/history/'+identifier).json()
                        assert final['session_id']==identifier and [m['sequence'] for m in history['messages']]==[1,2,3,4]
                        assert len({m['id'] for m in history['messages']})==4
                        state.update(history_digest=history_digest(history),message_count=4)
                        report['followup_context_preserved']=True
                async def binding():
                    db=configured_database()
                    try:
                        async with db.sessions() as s:
                            row=await s.get(ChatSession,identifier)
                            assert row.thread_id==state['thread_id'] and row.active_run_id is None
                            return True
                    finally:await db.close()
                report['thread_binding_preserved']=asyncio.run(binding())
                assert asyncio.run(business())==state['business_before']
                report.update(passed=True,business_tables_unchanged=True,session_id=identifier)
                state_path.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
    except Exception as error:report['error_type']=type(error).__name__
    (folder/(args.action+'.json')).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
