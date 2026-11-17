"""Verify real provider usage is connected to one persisted local run."""
import json
from pathlib import Path
from authenticated_client import authenticated_client
import httpx
from smart_cs.settings import PROJECT_ROOT


def main():
    report={'passed':False,'real_provider':True}
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=240) as client, authenticated_client(client) as identity:
            reply=client.post('/api/chat',json={'message':'请依据消费者权益保护法实施条例，说明自动续费应何时提醒。','user_id':'observability-verification'})
            assert reply.status_code==200
            result=reply.json();run_id=result['run_id']
            for header,field in [('x-request-id','request_id'),('x-trace-id','trace_id'),('x-run-id','run_id')]:assert reply.headers[header]==result[field]
            stored=client.get('/api/runs/'+run_id,params={'user_id':'observability-verification'})
            assert stored.status_code==200
            record=stored.json()
            assert record['recording_backend']=='database' and record['status']=='succeeded'
            assert record['run_id']==run_id and record['trace_id']==result['trace_id']
            assert len(record['nodes'])==5 and record['duration_ms']>0
            assert {c['kind'] for c in record['model_calls']}=={'chat','embedding','reranker'}
            assert len([c for c in record['model_calls'] if c['kind']=='chat'])==3
            for call in record['model_calls']:
                assert call['run_id']==run_id and call['duration_ms']>=0
                assert call['trace_id']==record['trace_id'] and call['span_id'] is not None
                if call['usage']['status']=='unknown':assert call['usage']['total_tokens'] is None
            assert record['identity']['authenticated'] and record['identity']['subject_id']==identity['id']
            with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False) as other, authenticated_client(other,'customer-b'):
                assert other.get('/api/runs/'+run_id,params={'user_id':identity['id']}).status_code==404
            invalid=client.post('/api/chat',json={'message':'   '})
            assert invalid.status_code==422 and invalid.json()['error']['code']=='VALIDATION_ERROR'
            report.update(passed=True,run_id=run_id,request_id=record['request_id'],trace_id=record['trace_id'],
                duration_ms=record['duration_ms'],usage=record['usage'],node_count=len(record['nodes']),
                calls=[{k:c[k] for k in ['kind','agent_role','operation','model','duration_ms','usage','span_id']} for c in record['model_calls']],
                query_url='http://127.0.0.1:8000/api/runs/'+run_id)
    except Exception as error:report['error_type']=type(error).__name__
    folder=PROJECT_ROOT/'.local/checks';folder.mkdir(parents=True,exist_ok=True)
    (folder/'observability-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
