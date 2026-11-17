"""Real-model approved create/close, committed-result replay and actual API restart.

prepare creates one synthetic customer-a refund ticket, then prepares and approves
its closure. Restart only this API, then resume within the consent validity window.
The final check permits exactly this ticket, its two events and the order's two
fact-version increments; original facts, other tickets and all vectors are checked.
"""
import argparse
import asyncio
import json
import httpx
from dotenv import load_dotenv
from sqlalchemy import select,text
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import Order,Ticket,TicketEvent,ActionExecution
from verify_core_schema import fingerprints
from authenticated_client import authenticated_client

OUT=PROJECT_ROOT/'.local/execution'
MUTABLE={'auth_sessions','auth_login_attempts','chat_sessions','chat_messages','chat_turns','execution_runs',
         'model_call_records','action_requests','approvals','action_executions'}


async def snapshot(ticket_id=None):
    db=configured_database()
    try:
        values=await fingerprints(db,exclude_order_ids=['ORDER-A-006'],exclude_ticket_ids=[ticket_id] if ticket_id else None)
        async with db.sessions() as s:
            row=(await s.execute(text("SELECT to_jsonb(t) FROM orders t WHERE id='ORDER-A-006'"))).scalar_one()
            active=(await s.execute(text('SELECT id,active,model_identity FROM retrieval_indexes ORDER BY id'))).all()
        return {'preserved':{k:v for k,v in values.items() if k not in MUTABLE},'order':row,'indexes':[list(r) for r in active]}
    finally:await db.close()


def final(response):
    assert response.status_code==200
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    errors=[e.get('code') for e in events if e['type']=='error']
    if errors:raise RuntimeError('Graph returned '+','.join(errors))
    return next(e for e in events if e['type']=='complete')


def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


async def replay(headers,identifier,parameter_hash):
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8000',headers=headers,trust_env=False,timeout=60) as client:
        replies=await asyncio.gather(*(client.post('/api/action-requests/'+identifier+'/execute',json={'parameter_hash':parameter_hash}) for _ in range(8)))
        assert all(r.status_code==200 for r in replies)
        results=[r.json() for r in replies];assert all(r==results[0] for r in results)
        return results[0]


def prepare(client,report):
    report.update(before=asyncio.run(snapshot()),cases=[])
    with authenticated_client(client,'customer-a'):
        workflow=client.get('/api/workflow').json();assert len([n for n in workflow['nodes'] if n.get('role')])==4
        assert any(n['id']=='execute_action' for n in workflow['nodes'])
        draft=final(client.post('/api/chat/stream',json={'message':'【第十七步登记验收】为本人订单 ORDER-A-006 登记100.00元退款申请，原因商品不合适。请生成普通优先级工单草稿并填写摘要、原因，等待我确认；不办理实际资金退款。'}))
        assert draft['compliance_passed'];view=draft['action_request'];assert view['status']=='awaiting_user'
        assert view['required_approval']=='user' and not draft['action_execution']
        assert client.post('/api/action-requests/'+view['action_id']+'/execute',json={'parameter_hash':view['parameter_hash']}).status_code==409
        assert client.post('/api/action-requests/'+view['action_id']+'/confirm',json={'parameter_hash':view['parameter_hash'],'decision':'approve'}).status_code==200
        created=final(client.post('/api/sessions/'+draft['session_id']+'/resume',json={}))
        assert created['compliance_passed'] and created['action_execution']['status']=='succeeded'
        result=created['action_execution'];assert result['tool']=='ticket_create' and result['reservation_change']=='100.00' and not result['funds_refunded']
        assert result==client.get('/api/action-requests/'+view['action_id']+'/execution').json()
        assert result==asyncio.run(replay(dict(client.headers),view['action_id'],view['parameter_hash']))
        report['created']={'session_id':draft['session_id'],'run_id':created['run_id'],'result':result}
        save('http-prepare.partial.json',report)
        print(json.dumps({'case':'create','ticket_id':result['ticket_id'],'committed':True,'replayed_requests':8}),flush=True)

    prepare_closing(client,report)


def prepare_closing(client,report):
    result=report['created']['result']
    with authenticated_client(client,'customer-a'):
        assert client.get('/api/action-requests/'+result['action_id']+'/execution').json()==result
        message=f"【第十七步流转验收】请为本人退款工单 {result['ticket_id']} 生成关闭申请，当前版本是 {result['ticket_version']}，目标状态closed，原因为撤销退款申请。只生成待确认、待人工审批的流转草稿。"
        closing=final(client.post('/api/chat/stream',json={'message':message}))
        assert closing['compliance_passed'];pending=closing['action_request']
        assert pending['required_approval']=='staff' and pending['status']=='awaiting_user'
        assert pending['tool']=='ticket_transition'
        assert client.post('/api/action-requests/'+pending['action_id']+'/confirm',json={'parameter_hash':pending['parameter_hash'],'decision':'approve'}).status_code==200
        assert client.post('/api/action-requests/'+pending['action_id']+'/execute',json={'parameter_hash':pending['parameter_hash']}).status_code==409
        report['closing']={'session_id':closing['session_id'],'action_request':pending}
    with authenticated_client(client,'support'):
        pending=report['closing']['action_request']
        response=client.post('/api/staff/action-requests/'+pending['action_id']+'/decide',json={'parameter_hash':pending['parameter_hash'],'decision':'approve'})
        assert response.status_code==200 and response.json()['status']=='approved'
    with authenticated_client(client,'customer-b'):
        assert client.get('/api/action-requests/'+result['action_id']+'/execution').status_code==404
    report.update(passed=True,awaiting_api_restart=True,real_models=True,business_write_performed=True)


def resume(client,report):
    saved=json.loads((OUT/'http-prepare.json').read_text(encoding='utf-8'));assert saved['passed']
    with authenticated_client(client,'customer-a'):
        created=saved['created']['result']
        assert client.get('/api/action-requests/'+created['action_id']+'/execution').json()==created
        assert asyncio.run(replay(dict(client.headers),created['action_id'],created['parameter_hash']))==created
        pending=saved['closing']['action_request']
        response=client.get('/api/history/'+saved['closing']['session_id']).json()
        assert response['can_resume'] and response['turn_status']=='waiting_approval'
        closed=final(client.post('/api/sessions/'+saved['closing']['session_id']+'/resume',json={}))
        assert closed['compliance_passed'];result=closed['action_execution']
        assert result['status']=='succeeded' and result['tool']=='ticket_transition'
        assert result['ticket_id']==created['ticket_id'] and result['ticket_version']==2 and result['ticket_status']=='closed'
        assert result['reservation_change']=='-100.00' and not result['funds_refunded']
        assert asyncio.run(replay(dict(client.headers),result['action_id'],result['parameter_hash']))==result
        assert client.get('/api/action-requests/'+result['action_id']+'/execution').json()==result
        assert client.post('/api/sessions/'+saved['closing']['session_id']+'/resume',json={}).status_code==409
        history=client.get('/api/history/'+saved['closing']['session_id']).json()
        assert history['can_continue'] and history['messages'][-1]['metadata']['action_execution']==result
    after=asyncio.run(snapshot(created['ticket_id']));before=saved['before']
    assert after['preserved']==before['preserved'] and after['indexes']==before['indexes']
    assert after['order']['fact_version']==before['order']['fact_version']+2
    assert {k:v for k,v in after['order'].items() if k!='fact_version'}=={k:v for k,v in before['order'].items() if k!='fact_version'}
    async def persisted():
        db=configured_database()
        try:
            async with db.sessions() as s:
                ticket=await s.get(Ticket,created['ticket_id'])
                events=(await s.scalars(select(TicketEvent).where(TicketEvent.ticket_id==ticket.id))).all()
                receipts=(await s.scalars(select(ActionExecution).where(ActionExecution.ticket_id==ticket.id))).all()
                assert ticket.status=='closed' and str(ticket.requested_amount)=='100.00' and ticket.version==2
                assert len(events)==len(receipts)==2 and {e.action_id for e in events}=={created['action_id'],result['action_id']}
        finally:await db.close()
    asyncio.run(persisted())
    report.update(passed=True,real_models=True,actual_restart=True,created=created,closed=result,
        receipt_queries_survive_restart=True,concurrent_replay_requests=24,exactly_one_ticket_two_events_two_receipts=True,
        order_reservation_round_trip=True,original_business_facts_and_all_indexes_preserved=True,
        allowed_order_fact_version_increment=2,old_approvals_not_used_for_new_actions=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('phase',choices=['prepare','prepare-close','resume']);args=parser.parse_args()
    load_dotenv(ENV_PATH);OUT.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'phase':args.phase}
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=300) as client:
            assert client.get('/health').json()['chat_ready']
            if args.phase=='prepare-close':
                report=json.loads((OUT/'http-prepare.partial.json').read_text(encoding='utf-8'))
                report.update(passed=False,phase='prepare-close')
                prepare_closing(client,report)
            else:(prepare if args.phase=='prepare' else resume)(client,report)
    except Exception as error:report.update(passed=False,error_type=type(error).__name__,safe_message=str(error) if isinstance(error,RuntimeError) else '')
    save('http-'+('prepare' if args.phase=='prepare-close' else args.phase)+'.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'before','cases','created','closing','closed'}},indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
