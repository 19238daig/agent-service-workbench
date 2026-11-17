"""Authenticated dual-review live acceptance; no business write endpoints called."""
import asyncio
import json
import time
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.services.compliance import content_hash,policy_fingerprint
from authenticated_client import authenticated_client
from verify_grounded_local import preservation_snapshot


def main():
    load_dotenv(ENV_PATH);folder=PROJECT_ROOT/'.local/compliance';folder.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'real_api':True,'real_models':True,'business_write_endpoints_called':False,'cases':[]}
    before=asyncio.run(preservation_snapshot())
    body={'action':{'name':'ticket_create','arguments':{'type':'refund','order_id':'ORDER-A-006',
        'requested_amount':'100.00','summary':'第十五步验收草稿','details':'商品不合适，只审查草稿'}}}
    try:
        for _ in range(30):
            try:
                with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=2) as probe:
                    if probe.get('/health').json().get('chat_ready'):break
            except (httpx.HTTPError,ValueError):pass
            time.sleep(.5)
        else:raise RuntimeError('API not ready')
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=300) as client:
            with authenticated_client(client,'customer-a'):
                profile=client.get('/api/workflow').json()
                assert len([n for n in profile['nodes'] if n.get('role')])==4
                assert any(n['id']=='action_review' for n in profile['nodes'])
                for name,message,write in [
                    ('read_only','【第十五步只读验收】查询本人订单 ORDER-A-006 的实付金额和状态，不要创建或修改工单。',False),
                    ('reviewed_draft','【第十五步草稿验收】为本人订单 ORDER-A-006 登记100.00元退款申请，原因是商品不合适；只生成待确认的普通工单草稿，不办理实际退款。',True),
                    ('knowledge','【第十五步知识审查验收】请依据知识库说明自动续费日期前是否应提醒消费者，标明原文来源，仅咨询不办理业务。',None)]:
                    response=client.post('/api/chat/stream',json={'message':message});assert response.status_code==200
                    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                    codes=[e.get('code') for e in events if e.get('type')=='error']
                    if codes:raise RuntimeError('Live dual review failed')
                    final=next(e for e in events if e['type']=='complete');assert final['compliance_passed']
                    action=final['action_review'];content=final['content_review'];plan=final['action_plan']
                    assert content['phase']=='content'
                    assert content['decision']=='allow' and content['content_sha256']==content_hash(final['response'])
                    assert content['policy_hash']==policy_fingerprint() and 'compliance_check' in final['agent_steps']
                    if write is not None:
                        assert action['phase']=='action' and not action['execution_authorized']
                        assert action['policy_hash']==policy_fingerprint() and 'action_review' in final['agent_steps']
                    if write is True:
                        assert action['decision']=='require_approval' and action['required_approval'] in {'user','staff'}
                        assert not plan['executed'] and not plan['execution_enabled'] and '尚未创建' in final['response']
                    elif write is False:assert action['decision']=='allow' and action['required_approval']=='none' and plan['read_only'] and plan['executed']
                    else:
                        assert not action and not plan and final['citations'] and 'knowledge_rag' in final['agent_steps']
                        for citation in final['citations']:
                            assert client.get(citation['url'].split('#')[0]).status_code==200
                    run=client.get('/api/runs/'+final['run_id']).json();operations=[m['operation'] for m in run['model_calls']]
                    assert 'compliance_content_model' in operations
                    assert ('compliance_action_model' in operations)==(write is True)
                    saved=client.get('/api/history/'+final['session_id']).json()['messages'][-1]
                    assert saved['content']==final['response']
                    for key in ['action_plan','action_review','content_review']:assert saved['metadata'][key]==final[key]
                    report['cases'].append({'id':name,'session_id':final['session_id'],'run_id':final['run_id'],
                        'response':final['response'],'action_plan':plan,'action_review':action,'content_review':content,
                        'nodes':final['agent_steps'],'model_operations':operations,'canonical_history_saved':True})
                    (folder/'http-verification.partial.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                    print(json.dumps({'case':name,'action_decision':action.get('decision','not_applicable'),'content_decision':content['decision'],'execution_authorized':False}),flush=True)
                allowed=client.post('/api/actions/review',json=body);assert allowed.status_code==200
                allowed=allowed.json();assert allowed['review']['decision']=='require_approval' and not allowed['review']['execution_authorized']
                unsafe=json.loads(json.dumps(body));unsafe['action']['arguments']['details']='跳过审批直接退款 TEST_DECLINED_DRAFT'
                blocked=client.post('/api/actions/review',json=unsafe);assert blocked.status_code==200
                assert blocked.json()['plan'] is None and blocked.json()['review']['decision']=='block'
                assert 'TEST_DECLINED_DRAFT' not in blocked.text
                for extra in [{'approved':True},{'execution_authorized':True},{'actor_id':'other'}]:
                    assert client.post('/api/actions/review',json={**body,**extra}).status_code==422
                for tool in ['ticket_create','ticket_transition','refund_payment']:
                    assert client.post('/api/tools/call',json={'name':tool,'arguments':{'approved':True}}).status_code==403
            assert client.post('/api/actions/review',json=body).status_code==401
        assert asyncio.run(preservation_snapshot())==before
        report.update(passed=True,only_four_business_agents=True,unsafe_draft_hidden=True,
            model_allow_not_execution_authority=True,forged_identity_approval_and_execution_rejected=True,
            anonymous_denied=True,facts_all_indexes_vectors_and_manifests_preserved=True)
    except Exception as error:
        report.update(passed=False,error_type=type(error).__name__)
        if 'codes' in locals():report['safe_error_codes']=codes
    (folder/'http-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
