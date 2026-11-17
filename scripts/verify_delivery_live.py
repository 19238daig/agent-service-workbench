"""Isolated restored app: real auth, worker, restart and optional provider RAG."""
import argparse,asyncio,json,time
from uuid import uuid4
from datetime import datetime,timezone
from dotenv import dotenv_values,set_key
from sqlalchemy import create_engine,text
import httpx
from delivery_drill import OUT,ENV,COMPOSE,url,assert_target,run,write,login
from smart_cs.settings import PROJECT_ROOT


def accounts():
    source=json.loads((OUT/'backup/source.json').read_text())
    return json.loads((PROJECT_ROOT/'.local/auth/bootstrap-accounts.json' if source['source_live'] else OUT/'fresh-accounts.json').read_text(encoding='utf-8'))


def scalar(statement):
    engine=create_engine(url().replace('+asyncpg','+psycopg',1),hide_parameters=True)
    try:
        with engine.connect() as c:return c.scalar(text(statement))
    finally:engine.dispose()


def wait(client,path,terminal,limit=180):
    start=time.monotonic()
    while time.monotonic()-start<limit:
        response=client.get(path);assert response.status_code==200;row=response.json()
        if (row.get('run_status') or row.get('status')) in terminal:return row
        time.sleep(.5)
    raise TimeoutError('Isolated delivery operation exceeded deadline')


def main(with_models,reuse_run=False):
    assert_target();assert dotenv_values(ENV)['DELIVERY_DATABASE']=='smart_cs_delivery_restored'
    assert scalar("SELECT count(*) FROM run_controls WHERE status IN ('queued','running','cancel_requested')")==0
    assert scalar("SELECT count(*) FROM background_jobs WHERE status IN ('queued','running')")==0
    before={'orders':scalar('SELECT count(*) FROM orders'),'tickets':scalar('SELECT count(*) FROM tickets'),'executions':scalar('SELECT count(*) FROM action_executions'),
        'active_vectors':scalar('SELECT count(*) FROM knowledge_embeddings e JOIN retrieval_indexes i ON i.id=e.index_id WHERE i.active')}
    set_key(ENV,'DELIVERY_RUN_EXECUTOR_ENABLED','true')
    run([*COMPOSE,'up','-d','--wait','app'],'live-executor-start')
    config=dotenv_values(ENV);base='http://127.0.0.1:'+config['DELIVERY_APP_PORT'];report={'passed':False,'source_workspace_untouched':True,'with_real_chat_models':with_models}
    a=accounts()
    with httpx.Client(base_url=base,trust_env=False,timeout=30,headers={'Origin':base}) as client:
        login(client,a)
        request_id='delivery-'+uuid4().hex;session=None
        if with_models:
            body={'message':'根据公开资料，经营者采用自动续费服务时应如何提醒消费者？只咨询政策。','client_request_id':request_id}
            if reuse_run:
                identifier=json.loads((OUT/'live-run-started.json').read_text())['run_id']
            else:
                if (OUT/'live-run-started.json').exists():raise ValueError('An earlier model run exists; explicitly reuse it instead of silently replaying')
                submitted=client.post('/api/runs',json=body);assert submitted.status_code==202
                identifier=submitted.json()['run_id'];write('live-run-started',{'run_id':identifier,'client_request_id':request_id,'replay_allowed':False})
            finished=wait(client,'/api/runs/'+identifier,{'succeeded','failed','interrupted','waiting_approval','cancelled'},240)
            write('live-run-result',finished);event=finished.get('result_event') or {}
            assert finished.get('run_status')=='succeeded' and event.get('type')=='complete'
            assert event.get('compliance_passed') and event.get('knowledge_result',{}).get('status')=='answered'
            assert event['citations']
            session=event['session_id'];saved=client.get('/api/history/'+session).json()
            stream=client.get('/api/runs/'+identifier+'/events',headers={'Last-Event-ID':'0'});assert stream.status_code==200 and 'event' in stream.text
            report.update(run_id=identifier,session_id=session,agent_steps=event['agent_steps'],citations=len(event['citations']),real_models_answered=True,existing_model_run_reused=reuse_run)
        calls=scalar('SELECT count(*) FROM model_call_records')
        run([*COMPOSE,'restart','app'],'live-app-restart')
        run([*COMPOSE,'up','-d','--wait','app'],'live-app-ready-after-restart')
        assert client.get('/api/orders/ORDER-A-006').status_code==200
        if with_models:
            assert client.get('/api/history/'+session).json()==saved
            replayed=client.get('/api/runs/'+identifier+'/events',headers={'Last-Event-ID':'0'})
            assert replayed.status_code==200
            assert client.get('/api/runs/'+identifier).json()['result_event']==event
        assert scalar('SELECT count(*) FROM model_call_records')==calls,'Reading restored history or replaying SSE invoked models'
        report.update(restart_preserves_history_and_events=True,reading_history_replayed_model_calls=0)
        client.headers.pop('Authorization');client.cookies.clear();login(client,a,'admin')
        raw=b'Isolated delivery worker test. This draft is not published and never enters the active knowledge index.'
        artifact=client.post('/api/admin/job-artifacts?filename=delivery-worker-control.txt',content=raw,headers={'Content-Type':'application/octet-stream'});assert artifact.status_code==201
        artifact=artifact.json();stamp=uuid4().hex
        body={'idempotency_key':'delivery-worker-'+stamp,'work':{'kind':'ingest','artifact_sha256':artifact['artifact_sha256'],'extension':'.txt',
            'document':{'source_key':'delivery-worker-'+stamp,'title':'Isolated worker control','version':'v1','domain':'evaluation_control','access_scope':'admin',
              'effective_from':datetime.now(timezone.utc).date().isoformat(),'source_type':'synthetic_delivery_fixture','publisher':'Local verification',
              'license':'Private synthetic control'},'options':{'chunker':'structural'},'publish':False}}
        queued=client.post('/api/admin/jobs',json=body);assert queued.status_code==202;job=queued.json();write('worker-job-started',job)
        # The job exists in PostgreSQL while worker/dispatcher are stopped.
        run([*COMPOSE,'--profile','jobs','up','-d','worker','dispatcher'],'live-workers-start')
        complete=wait(client,'/api/admin/jobs/'+job['id'],{'succeeded','failed','cancelled'},180)
        write('worker-job-result',complete);assert complete['status']=='succeeded'
        again=client.post('/api/admin/jobs',json=body);assert again.status_code==202 and again.json()['id']==job['id']
        report.update(real_worker_completed=True,job_id=job['id'],job_submission_idempotent=True,ingestion_published=False)
    after={'orders':scalar('SELECT count(*) FROM orders'),'tickets':scalar('SELECT count(*) FROM tickets'),'executions':scalar('SELECT count(*) FROM action_executions'),
        'active_vectors':scalar('SELECT count(*) FROM knowledge_embeddings e JOIN retrieval_indexes i ON i.id=e.index_id WHERE i.active')}
    assert after==before
    report.update(passed=True,business_counts_and_active_index_unchanged=after,model_records_after_read_replay=calls)
    write('live-verification',report);print(json.dumps({'passed':True,'real_chat_models':with_models,'real_worker':True,'restart_and_sse_replay_no_model_calls':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--with-models',action='store_true');parser.add_argument('--reuse-run',action='store_true');args=parser.parse_args()
    try:main(args.with_models,args.reuse_run)
    except Exception as error:
        write('live-verification-failure',{'passed':False,'error_type':type(error).__name__});print(json.dumps({'passed':False,'error_type':type(error).__name__}));raise SystemExit(1)
