"""Local ticket acceptance. Writes only a new labelled synthetic case when requested."""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from authenticated_client import authenticated_client
from verify_core_schema import fingerprints


async def business_snapshot(exclude=None):
    db=configured_database()
    try:
        tables=await fingerprints(db,exclude_ticket_ids=exclude)
        return {name:tables[name] for name in ['orders','tickets','ticket_events']}
    finally:await db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    operations=parser.add_mutually_exclusive_group()
    operations.add_argument('--exercise-manual-writes',action='store_true')
    parser.add_argument('--chat',action='store_true')
    parser.add_argument('--verify-restart',action='store_true')
    operations.add_argument('--reuse-created-case',action='store_true',help='Recheck the previously recorded synthetic case without creating another')
    args=parser.parse_args();load_dotenv(ENV_PATH)
    if args.verify_restart and (args.exercise_manual_writes or args.reuse_created_case or args.chat):
        parser.error('--verify-restart must be run by itself')
    output=PROJECT_ROOT/'.local/tickets';output.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'real_api':True,'real_model':args.chat}
    try:
        with ExitStack() as stack:
            a,b,staff,admin=[stack.enter_context(httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=240)) for _ in range(4)]
            owner=stack.enter_context(authenticated_client(a,'customer-a'))
            stack.enter_context(authenticated_client(b,'customer-b'))
            employee=stack.enter_context(authenticated_client(staff,'support'))
            stack.enter_context(authenticated_client(admin,'admin'))
            if args.verify_restart:
                previous=json.loads((output/'live-http-verification.json').read_text(encoding='utf-8'))
                identifier=previous['created_ticket_id'];row=a.get('/api/tickets/'+identifier).json()['ticket']
                events=a.get('/api/tickets/'+identifier+'/events').json()['events']
                assert row['status']=='closed' and row['version']==5 and [e['ticket_version'] for e in events]==[1,2,3,4,5]
                report.update(passed=True,restart_persistence=True,ticket_id=identifier,events=5)
            else:
                before=asyncio.run(business_snapshot())
                sample='TK-20261114-9A0001'
                assert a.get('/api/tickets/'+sample).status_code==200
                assert staff.get('/api/tickets/'+sample).status_code==200
                assert b.get('/api/tickets/'+sample+'/events',params={'user_id':owner['id']}).status_code==404
                assert admin.get('/api/tickets/'+sample).status_code==404
                identifier=None
                if args.reuse_created_case:
                    previous=json.loads((output/'live-http-verification.json').read_text(encoding='utf-8'))
                    identifier=previous['created_ticket_id']
                    row=a.get('/api/tickets/'+identifier).json()['ticket']
                    events=a.get('/api/tickets/'+identifier+'/events').json()['events']
                    assert row['status']=='closed' and row['version']==5 and [e['ticket_version'] for e in events]==[1,2,3,4,5]
                    assert [e['actor_id'] for e in events]==[owner['id'],owner['id'],employee['id'],employee['id'],owner['id']]
                    report.update(created_ticket_id=identifier,event_versions=[1,2,3,4,5],actual_actors_recorded=True,final_status='closed',reused_existing_case=True)
                if args.exercise_manual_writes:
                    created=a.post('/api/tickets',json={'type':'refund','summary':'合成联调验证：工单持久化闭环',
                        'details':'仅用于第九步验收，不对应真实退款操作。','order_id':'ORDER-A-006','confirm_registration':True})
                    assert created.status_code==201;row=created.json()['ticket'];identifier=row['ticket_id'];path='/api/tickets/'+identifier
                    assert row['user_id']==owner['id'] and row['source']=='manual_api'
                    assert staff.get(path).status_code==404
                    row=a.post(path+'/assignment',json={'assignee_id':employee['id'],'expected_version':1,'reason':'合成用户明确选择客服'}).json()['ticket']
                    assert a.post(path+'/transitions',json={'target_status':'processing','expected_version':2,'reason':'不应允许客户冒充客服'}).status_code==403
                    assert staff.post(path+'/transitions',json={'target_status':'resolved','expected_version':2,'reason':'不应跳过合法状态'}).status_code==409
                    for target in ['processing','resolved']:
                        response=staff.post(path+'/transitions',json={'target_status':target,'expected_version':row['version'],'reason':'合成客服处理记录'})
                        assert response.status_code==200;row=response.json()['ticket']
                    assert staff.post(path+'/transitions',json={'target_status':'closed','expected_version':2,'reason':'过期版本'}).status_code==409
                    row=a.post(path+'/transitions',json={'target_status':'closed','expected_version':row['version'],'reason':'合成用户确认关闭'}).json()['ticket']
                    assert row['status']=='closed' and row['version']==5
                    events=a.get(path+'/events').json()['events']
                    assert [e['ticket_version'] for e in events]==[1,2,3,4,5]
                    assert [e['actor_id'] for e in events]==[owner['id'],owner['id'],employee['id'],employee['id'],owner['id']]
                    report.update(created_ticket_id=identifier,event_versions=[1,2,3,4,5],actual_actors_recorded=True,final_status='closed')
                if args.chat:
                    query=identifier or sample
                    response=a.post('/api/chat/stream',json={'message':f'请只查询工单 {query} 的当前处理状态，不要创建或修改工单。'})
                    assert response.status_code==200
                    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                    final=next(e for e in events if e['type']=='complete')
                    report.update(run_id=final['run_id'],nodes=[item['node'] for item in final['execution_trace']],
                        query_verified=bool(final['compliance_passed'] and query in final['response']),compliance_passed=final['compliance_passed'])
                    assert final['compliance_passed'] and query in final['response']
                    if identifier:assert '已关闭' in final['response']
                    report.update(run_id=final['run_id'],nodes=[item['node'] for item in final['execution_trace']])
                assert a.post('/api/tools/call',json={'name':'ticket_create','arguments':{}}).status_code==403
                assert a.post('/api/tools/call',json={'name':'ticket_transition','arguments':{}}).status_code==403
                after=asyncio.run(business_snapshot([identifier] if identifier and args.exercise_manual_writes else None))
                assert before==after
                report.update(passed=True,preexisting_business_facts_unchanged=True,cross_user_denied=True,unassigned_admin_denied=True,agent_write_tools_denied=True)
    except Exception as error:report['error_type']=type(error).__name__
    target='restart-verification.json' if args.verify_restart else 'live-http-verification.json'
    (output/target).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
