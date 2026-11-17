"""Three real read-only SSE runs per phase. Measure progress separately from final reply.

Run B3 BEFORE restarting the old API, B4 AFTER deployment. No business actions.
"""
import argparse,json,time
from time import perf_counter
from uuid import uuid4
from dotenv import load_dotenv
import httpx
from authenticated_client import authenticated_client
from smart_cs.settings import ENV_PATH
from evaluate_performance import OUT,read,write

FIRST='根据消费者权益保护法实施条例，经营者在自动续费前应如何提醒消费者？'
FOLLOW='根据公开资料，经营者在自动续费前应如何向消费者作出提醒？'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['B3','B4']);args=p.parse_args();load_dotenv(ENV_PATH)
    assert not (OUT/('live-'+args.phase+'-summary.json')).exists(),'Existing experiment is immutable; use a new run directory'
    results=[]
    with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=240) as client:
        with authenticated_client(client,'customer-a'):
            for i in range(3):
                marker=OUT/f'live-{args.phase}-{i}-request.json'
                if marker.exists():body=read(marker)
                else:
                    body={'message':FIRST if i<2 else FOLLOW,'client_request_id':'step25-'+args.phase+'-'+uuid4().hex,
                          'session_id':results[0]['session_id'] if i==2 else None};write(marker,body)
                started=perf_counter();response=client.post('/api/runs',json=body);response.raise_for_status();created=response.json()
                first=None;last=None;identifier=None;data=[]
                with client.stream('GET','/api/runs/'+created['run_id']+'/events') as stream:
                    stream.raise_for_status()
                    for line in stream.iter_lines():
                        if line.startswith('data:'):data.append(line[5:].lstrip())
                        elif not line and data:
                            value=json.loads('\n'.join(data));data=[]
                            if first is None and value['type'] in {'queued','running','node_start'}:first=(perf_counter()-started)*1000
                            if value['type'] in {'complete','error'}:last=value;break
                elapsed=(perf_counter()-started)*1000
                assert last is not None and last['type']=='complete', 'Live run failed; retained on server, do not replay blindly'
                assert not last.get('action_execution') and not last.get('action_request')
                state=client.get('/api/runs/'+created['run_id']);state.raise_for_status();record=state.json()
                row={'phase':args.phase,'case':i,'run_id':created['run_id'],'session_id':last['session_id'],
                     'first_persisted_progress_ms':first,'final_client_ms':elapsed,'server_duration_ms':record['duration_ms'],
                     'knowledge_status':last.get('knowledge_result',{}),'citations':len(last.get('citations',[])),
                     'calls':record['model_calls'],'usage':record['usage'],'performance':record.get('performance',{}),
                     'business_writes':False,'baseline_hidden_sdk_retries_unknown':args.phase=='B3'}
                write(OUT/f'live-{args.phase}-{i}.json',row);results.append(row)
                print(json.dumps({'phase':args.phase,'completed':i+1,'chat_attempts':sum(c['kind']=='chat' for c in row['calls']),
                                  'local_attempts':sum(c['kind']!='chat' for c in row['calls']),
                                  'cache_hits':row['performance'].get('cache_hits'),'rewrite_skipped':row['performance'].get('rewrite_skipped'),
                                  'status':row['knowledge_status'].get('status')}),flush=True)
        if args.phase=='B4':
            assert results[1]['performance']['cache_hits']>=2
            assert results[2]['performance']['rewrite_skipped']>=1
            with authenticated_client(client,'customer-a'):assert client.get('/api/admin/performance').status_code==403
            with authenticated_client(client,'admin'):
                r=client.get('/api/admin/performance');r.raise_for_status();write(OUT/'live-dashboard.json',r.json())
            assert client.get('/health/ready').status_code==200
    write(OUT/('live-'+args.phase+'-summary.json'),{'passed':True,'runs':results,
          'scope':'three read-only real provider samples; not p95/throughput or independent answer accuracy'})


if __name__=='__main__':main()
