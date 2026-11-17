"""Real authenticated read/planning acceptance. Never invoke manual write routes."""
import asyncio
import json
import time
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from authenticated_client import authenticated_client
from verify_grounded_local import preservation_snapshot


def main():
    load_dotenv(ENV_PATH);folder=PROJECT_ROOT/'.local/actions';folder.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'real_models':True,'business_write_routes_called':False,'cases':[]}
    before=asyncio.run(preservation_snapshot())
    proposal={'action':{'name':'ticket_create','arguments':{'type':'refund','order_id':'ORDER-A-006',
        'requested_amount':'100.00','summary':'第十四步验收草稿','details':'商品不合适，仅校验草稿'}}}
    try:
        # Wait only for startup; never automatically replay a chat request.
        for attempt in range(30):
            try:
                with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=2) as probe:
                    if probe.get('/health').json().get('chat_ready'):break
            except (httpx.HTTPError,ValueError):pass
            time.sleep(.5)
        else:raise RuntimeError('API not ready')
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=300) as client:
            assert client.get('/health').json()['chat_ready']
            with authenticated_client(client,'customer-a') as user:
                catalog=client.get('/api/tools').json()['tools'];assert len(catalog)==7
                assert all(not t['execution_enabled'] for t in catalog if not t['read_only'])
                valid=client.post('/api/actions/validate',json=proposal);assert valid.status_code==200
                assert valid.headers['cache-control']=='no-store'
                valid=valid.json();assert valid['status']=='draft_validated' and valid['actor_id']==user['id']
                assert not valid['executed'] and not valid['execution_enabled']
                missing=json.loads(json.dumps(proposal));missing['action']['arguments']['order_id']=None
                result=client.post('/api/actions/validate',json=missing).json()
                assert result['status']=='needs_information' and 'order_id' in result['missing_fields']
                excessive=json.loads(json.dumps(proposal));excessive['action']['arguments']['requested_amount']='999999.00'
                result=client.post('/api/actions/validate',json=excessive).json()
                assert result['status']=='rejected' and 'REQUEST_EXCEEDS_MAXIMUM' in result['reason_codes']
                for extra in [{'actor_id':'someone-else'},{'action_id':'forged'},{'approved':True},{'execute':True}]:
                    assert client.post('/api/actions/validate',json={**proposal,**extra}).status_code==422
                for name in ['ticket_create','ticket_transition','refund_payment']:
                    assert client.post('/api/tools/call',json={'name':name,'arguments':{'approved':True}}).status_code==403
                ticket=client.post('/api/tools/call',json={'name':'ticket_query','arguments':{'ticket_id':'TK-20261114-9A0001'}})
                assert ticket.status_code==200
                assert client.post('/api/tools/call',json={'name':'ticket_events','arguments':{'ticket_id':'TK-20261114-9A0001','limit':1}}).status_code==200
                for identifier,message,expected in [
                    ('read_order','【第十四步只读验收】查询本人订单 ORDER-A-006 的实付金额和状态，不要创建或修改工单。','read_ready'),
                    ('refund_draft','【第十四步草稿验收】请为本人订单 ORDER-A-006 登记退款申请100.00元，原因是商品不合适，只生成退款工单草稿，不办理实际退款。','draft_validated')]:
                    response=client.post('/api/chat/stream',json={'message':message});assert response.status_code==200
                    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                    errors=[e.get('code') for e in events if e.get('type')=='error']
                    if errors:raise RuntimeError('Action chat execution failed')
                    final=next(e for e in events if e['type']=='complete')
                    assert final['compliance_passed'] and 'ticket_handler' in final['agent_steps']
                    plan=final['action_plan'];assert plan['protocol']=='action-plan-v1' and plan['status']==expected
                    assert plan['actor_id']==user['id'] and plan['action_id'].startswith('act-') and len(plan['parameter_hash'])==64
                    if expected=='draft_validated':
                        assert not plan['read_only'] and not plan['executed'] and not plan['execution_enabled']
                        assert plan['parameters']['requested_amount']=='100.00' and '尚未创建' in final['response']
                    else:assert plan['read_only'] and plan['executed'] and '500.00' in final['response']
                    saved=client.get('/api/history/'+final['session_id']).json()['messages'][-1]
                    assert saved['content']==final['response'] and saved['metadata']['action_plan']==plan
                    report['cases'].append({'id':identifier,'session_id':final['session_id'],'run_id':final['run_id'],
                        'response':final['response'],'action_plan':plan,'nodes':final['agent_steps'],'canonical_history_saved':True})
                    (folder/'http-verification.partial.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                    print(json.dumps({'case':identifier,'status':plan['status'],'executed':plan['executed']}),flush=True)
            with authenticated_client(client,'customer-b'):
                denied=client.post('/api/actions/validate',json=proposal).json()
                assert denied['status']=='rejected' and denied['reason_codes']==['ORDER_NOT_FOUND'] and not denied['fact_versions']
            assert client.post('/api/actions/validate',json=proposal).status_code==401
        assert asyncio.run(preservation_snapshot())==before
        report.update(passed=True,facts_all_indexes_vectors_and_manifests_preserved=True,
            missing_slots_rejected=True,foreign_order_denied=True,forged_identity_approval_and_execution_rejected=True,
            write_dispatch_denied=True,anonymous_denied=True,catalog_tools=7)
    except Exception as error:
        report.update(passed=False,error_type=type(error).__name__)
        if 'errors' in locals():report['safe_error_codes']=errors
    (folder/'http-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
