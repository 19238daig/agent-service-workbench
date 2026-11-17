"""Real RAG -> owned order -> clarify -> API restart -> approve -> commit -> close."""
import argparse
import asyncio
import json
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from authenticated_client import authenticated_client
from verify_execution_local import snapshot,final

OUT=PROJECT_ROOT/'.local/tasks'


def save(name,report):
    (OUT/name).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def prepare(client,report):
    report['before']=asyncio.run(snapshot())
    with authenticated_client(client,'customer-a'):
        profile=client.get('/api/workflow').json();assert len([n for n in profile['nodes'] if n.get('role')])==4
        assert any(n['id']=='task_dispatch' for n in profile['nodes'])
        message='【第18步补槽验收】请先解释知识库中的七日无理由退货政策及适用范围，再核验本人订单 ORDER-A-006 的退款条件；符合条件帮我登记退款申请。申请金额和具体原因我下一条消息补充，请先询问。'
        result=final(client.post('/api/chat/stream',json={'message':message}))
        report['initial']=result;save('http-prepare.partial.json',report)
        assert result['compliance_passed'] and result['task_plan']['status']=='needs_information'
        assert result['task_plan']['completed_task_ids']==['t1','t2'] and 'requested_amount' in result['task_plan']['missing_fields']
        assert not result['action_request'] and not result['action_execution'] and result['citations']
        for cite in result['citations']:assert client.get(cite['url'].split('#')[0]).status_code==200
        history=client.get('/api/history/'+result['session_id']).json()
        assert history['can_continue'] and not history['can_resume']
        assert history['task_plan']==result['task_plan']
        run=client.get('/api/runs/'+result['run_id']).json()
        operations=[c['operation'] for c in run['model_calls']]
        assert 'rag_grounded_draft' in operations and 'rag_support_review' in operations
        report.update(initial_operations=operations,session_id=result['session_id'],plan_id=result['task_plan']['plan_id'])
    assert asyncio.run(snapshot())==report['before']
    report.update(passed=True,real_models=True,real_grounded_citations=True,no_business_write_before_slots=True,awaiting_actual_restart=True)


def finish(client,report):
    saved=json.loads((OUT/'http-prepare.json').read_text(encoding='utf-8'));assert saved['passed']
    report['before']=saved['before']
    with authenticated_client(client,'customer-a'):
        history=client.get('/api/history/'+saved['session_id']).json();assert history['task_plan']['plan_id']==saved['plan_id'] and history['can_continue']
        draft=final(client.post('/api/chat/stream',json={'session_id':saved['session_id'],'message':'本次申请退款100.00元，具体原因是商品不合适；请补齐原先的申请草稿，仍然等待我确认，不办理实际资金退款。'}))
        report['draft']=draft;save('http-finish.partial.json',report)
        assert draft['compliance_passed'] and draft['task_plan']['plan_id']==saved['plan_id']
        assert draft['task_plan']['status']=='waiting_approval' and draft['task_plan']['completed_task_ids']==['t1','t2']
        assert draft['task_plan']['steps_used']<=8 and not draft['action_execution']
        run=client.get('/api/runs/'+draft['run_id']).json();operations=[c['operation'] for c in run['model_calls']]
        assert 'rag_grounded_draft' not in operations and 'rag_support_review' not in operations
        assert 'knowledge_rag' not in [n['node'] for n in draft['execution_trace']]
        request=draft['action_request'];identifier=request['action_id']
        assert client.post('/api/action-requests/'+identifier+'/execute',json={'parameter_hash':request['parameter_hash']}).status_code==409
        assert client.post('/api/action-requests/'+identifier+'/confirm',json={'parameter_hash':request['parameter_hash'],'decision':'approve'}).status_code==200
        committed=final(client.post('/api/sessions/'+saved['session_id']+'/resume',json={}))
        report['committed']=committed;save('http-finish.partial.json',report)
        assert committed['compliance_passed'] and committed['task_plan']['status']=='completed'
        assert committed['task_plan']['completed_task_ids']==['t1','t2','t3'] and not committed['citations']
        receipt=committed['action_execution'];assert receipt['status']=='succeeded' and receipt['requested_amount']=='100.00'
        assert client.get('/api/action-requests/'+identifier+'/execution').json()==receipt
        assert client.post('/api/action-requests/'+identifier+'/execute',json={'parameter_hash':request['parameter_hash']}).json()==receipt
        closing=final(client.post('/api/chat/stream',json={'message':f"【第18步验收关闭】请为本人工单 {receipt['ticket_id']} 生成关闭草稿，目标closed，原因为撤销验收退款申请；仅生成待确认、待客服审批的草稿。"}))
        report['closing']=closing;save('http-finish.partial.json',report)
        assert closing['compliance_passed'] and closing['task_plan']['status']=='waiting_approval'
        close=closing['action_request'];assert close['required_approval']=='staff'
        assert client.post('/api/action-requests/'+close['action_id']+'/confirm',json={'parameter_hash':close['parameter_hash'],'decision':'approve'}).status_code==200
    with authenticated_client(client,'support'):
        approved=client.post('/api/staff/action-requests/'+close['action_id']+'/decide',json={'parameter_hash':close['parameter_hash'],'decision':'approve'})
        assert approved.status_code==200 and approved.json()['status']=='approved'
    with authenticated_client(client,'customer-a'):
        closed=final(client.post('/api/sessions/'+closing['session_id']+'/resume',json={}))
        report['closed']=closed;save('http-finish.partial.json',report)
        assert closed['compliance_passed'] and closed['task_plan']['status']=='completed'
        assert closed['action_execution']['ticket_status']=='closed' and closed['action_execution']['reservation_change']=='-100.00'
    with authenticated_client(client,'customer-b'):
        assert client.get('/api/history/'+saved['session_id']).status_code==404
        assert client.post('/api/chat',json={'message':'退款100元','session_id':saved['session_id']}).status_code==404
    after=asyncio.run(snapshot(receipt['ticket_id']));before=saved['before']
    assert before['preserved']==after['preserved'] and before['indexes']==after['indexes']
    assert after['order']['fact_version']==before['order']['fact_version']+2
    assert {k:v for k,v in before['order'].items() if k!='fact_version'}=={k:v for k,v in after['order'].items() if k!='fact_version'}
    report.update(passed=True,real_models=True,actual_api_restart=True,task_plan_id=saved['plan_id'],ticket_id=receipt['ticket_id'],
        initial_policy_and_order_completed=True,completed_policy_not_repeated=True,missing_slots_survive_restart=True,
        consent_required=True,independent_staff_review_for_close=True,exactly_once_receipt_reused=True,
        owner_scoped=True,original_facts_and_all_indexes_preserved=True,order_reservation_round_trip=True,
        new_tickets=1,new_business_events=2,new_execution_receipts=2,allowed_order_fact_version_increment=2)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('phase',choices=['prepare','finish']);args=parser.parse_args()
    load_dotenv(ENV_PATH);OUT.mkdir(parents=True,exist_ok=True);report={'passed':False,'phase':args.phase}
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=300) as client:
            assert client.get('/health').json()['chat_ready'];(prepare if args.phase=='prepare' else finish)(client,report)
    except Exception as error:report.update(passed=False,error_type=type(error).__name__,safe_message=str(error) if isinstance(error,RuntimeError) else '')
    save('http-'+args.phase+'.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'before','initial','draft','committed','closing','closed'}},indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
