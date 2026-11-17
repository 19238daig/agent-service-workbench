"""Real-model draft -> durable human consent -> restart -> owner-scoped resume.

Run prepare, restart the verified project API, then run resume within five minutes.
No ticket registration/transition/payment endpoint is called.
"""
import argparse
import asyncio
import json
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from authenticated_client import authenticated_client
from verify_grounded_local import preservation_snapshot

FOLDER=PROJECT_ROOT/'.local/approvals'


def events(response):
    assert response.status_code==200
    result=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    errors=[r.get('code') for r in result if r['type']=='error']
    if errors:raise RuntimeError('Graph returned '+','.join(errors))
    return next(r for r in result if r['type']=='complete')


def prepare(client,report):
    report.update(before=asyncio.run(preservation_snapshot()),cases=[])
    with authenticated_client(client,'customer-a'):
        profile=client.get('/api/workflow').json()
        assert len([n for n in profile['nodes'] if n.get('role')])==4
        assert any(n['id']=='approval_gate' for n in profile['nodes'])
        for name,priority in [('normal','普通'),('staff','紧急 urgent')]:
            final=events(client.post('/api/chat/stream',json={'message':
                f'【第十六步{name}验收】为本人订单 ORDER-A-006 申请退款100.00元，原因商品不合适。请生成{priority}优先级的退款工单草稿，填写摘要和具体原因，等待我确认，不办理实际资金退款。'}))
            assert final['compliance_passed'] and final['action_plan'] and not final['action_plan']['read_only']
            view=final['action_request'];identifier=view['action_id']
            assert view['status']=='awaiting_user' and not view['executed'] and not view['execution_enabled']
            assert view['required_approval']==('user' if name=='normal' else 'staff')
            history=client.get('/api/history/'+final['session_id']).json()
            assert history['turn_status']=='waiting_approval' and not history['can_resume']
            assert history['messages'][-1]['metadata']['action_request']['action_id']==identifier
            assert client.post('/api/sessions/'+final['session_id']+'/resume',json={}).status_code==409
            report['cases'].append({'name':name,'session_id':final['session_id'],'turn_id':final['turn_id'],
                'initial_run_id':final['run_id'],'action_request':view,'response':final['response']})
            print(json.dumps({'case':name,'status':view['status'],'requirement':view['required_approval']}),flush=True)
        for case in report['cases']:
            view=case['action_request'];identifier=view['action_id'];body={'parameter_hash':view['parameter_hash'],'decision':'approve'}
            url='/api/action-requests/'+identifier+'/confirm'
            for extra in [{'approved':True},{'principal_id':'other'},{'expires_at':'2099'},{'parameters':{}}]:
                assert client.post(url,json={**body,**extra}).status_code==422
            assert client.post(url,json={**body,'parameter_hash':'f'*64}).status_code==409
            decision=client.post(url,json=body);assert decision.status_code==200
            assert decision.headers['Cache-Control']=='no-store'
            if case['name']=='staff':
                assert decision.json()['status']=='awaiting_staff'
                assert client.post('/api/sessions/'+case['session_id']+'/resume',json={}).status_code==409
            else:assert decision.json()['status']=='approved'
    with authenticated_client(client,'customer-b'):
        for case in report['cases']:
            identifier=case['action_request']['action_id']
            assert client.get('/api/action-requests/'+identifier).status_code==404
            assert client.post('/api/sessions/'+case['session_id']+'/resume',json={}).status_code==404
        assert client.get('/api/staff/action-requests').status_code==403
    with authenticated_client(client,'support'):
        case=next(c for c in report['cases'] if c['name']=='staff');view=case['action_request']
        queue=client.get('/api/staff/action-requests').json()['items'];assert any(r['action_id']==view['action_id'] for r in queue)
        response=client.post('/api/staff/action-requests/'+view['action_id']+'/decide',json={'parameter_hash':view['parameter_hash'],'decision':'approve'})
        assert response.status_code==200 and response.json()['status']=='approved'
    assert asyncio.run(preservation_snapshot())==report['before']
    report.update(passed=True,waiting_persisted=True,confirmation_and_distinct_staff_approval_recorded=True,
        business_facts_indexes_vectors_preserved=True,awaiting_restart=True)


def resume(client,report):
    saved=json.loads((FOLDER/'http-prepare.json').read_text(encoding='utf-8'))
    assert saved['passed'];report['cases']=[]
    with authenticated_client(client,'customer-a'):
        for case in saved['cases']:
            identifier=case['action_request']['action_id'];view=client.get('/api/action-requests/'+identifier).json()
            assert view['status']=='approved' and len(view['approvals'])==(1 if case['name']=='normal' else 2)
            assert view['parameter_hash']==case['action_request']['parameter_hash']
            history=client.get('/api/history/'+case['session_id']).json()
            assert history['turn_status']=='waiting_approval' and history['can_resume'] and not history['can_continue']
            final=events(client.post('/api/sessions/'+case['session_id']+'/resume',json={}))
            assert final['action_request']['status']=='ready' and not final['action_request']['executed']
            assert final['response']==case['response'] and final['turn_id']==case['turn_id']
            run=client.get('/api/runs/'+final['run_id']).json();assert not run['model_calls']
            assert client.post('/api/sessions/'+case['session_id']+'/resume',json={}).status_code==409
            history=client.get('/api/history/'+case['session_id']).json()
            assert history['can_continue'] and not history['can_resume'] and history['turn_status']=='completed'
            assert history['messages'][-1]['metadata']['action_request']['status']=='ready'
            report['cases'].append({'name':case['name'],'action_id':identifier,'session_id':case['session_id'],
                'run_id':final['run_id'],'status':'ready','executed':False,'model_calls_on_resume':0,'approval_count':len(view['approvals'])})
    assert asyncio.run(preservation_snapshot())==saved['before']
    report.update(passed=True,approval_wait_survived_restart=True,exact_parameters_and_approvals_preserved=True,
        no_repeated_models=True,no_business_writes=True,business_facts_indexes_vectors_preserved=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('phase',choices=['prepare','resume']);args=parser.parse_args()
    load_dotenv(ENV_PATH);FOLDER.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'phase':args.phase,'real_models':True,'business_write_endpoints_called':False}
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=300) as client:
            assert client.get('/health').json()['chat_ready']
            (prepare if args.phase=='prepare' else resume)(client,report)
    except Exception as error: report.update(passed=False,error_type=type(error).__name__,safe_message=str(error) if isinstance(error,RuntimeError) else '')
    (FOLDER/('http-'+args.phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'before','cases'}},indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
