"""Local real-model acceptance; only verified project API processes are restarted.

Reads synthetic ORDER-A-001/public knowledge. Adds chat/run/audit records only;
does not approve, execute or mutate tickets/orders. Never saves credentials.
"""
import argparse
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4
import httpx
from dotenv import load_dotenv
from sqlalchemy import select,text,inspect
from smart_cs.db.models import Base
from smart_cs.db.database import configured_database
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from authenticated_client import authenticated_client

OUT=PROJECT_ROOT/'.local/durable-runs'
MESSAGE='【第20步验收】请先根据公开资料解释哪些商品不适用七日无理由退货，再只查询订单 ORDER-A-001 的支付金额和当前状态。不要申请退款，不要创建或修改工单。'
def save(name,value):(OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def powershell(code):
    # Background children may inherit a pipe handle on Windows. Wait for the
    # shell process, rather than for pipe EOF from a long-lived API descendant.
    folder=OUT/'process-operations';folder.mkdir(parents=True,exist_ok=True);key=uuid4().hex
    stdout=folder/(key+'.out');stderr=folder/(key+'.err')
    with stdout.open('wb') as out,stderr.open('wb') as err:
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',code],cwd=PROJECT_ROOT,stdout=out,stderr=err,timeout=60)
    if result.returncode:raise RuntimeError('Verified API process operation failed')
    return stdout.read_bytes().decode('utf-8-sig',errors='replace').strip()

VERIFY=r'''
$taskRoot=(Get-Location).Path
$launcherId=[int](Get-Content -LiteralPath (Join-Path $taskRoot '.local\server.pid'))
$launcher=Get-CimInstance Win32_Process -Filter "ProcessId=$launcherId"
if($launcher.ExecutablePath -ne (Join-Path $taskRoot '.venv\Scripts\python.exe') -or $launcher.CommandLine -notlike '*start.py*'){throw 'Unexpected API launcher'}
$projectChildren=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$launcherId" | Where-Object {$_.Name -eq 'python.exe' -and $_.CommandLine -like '*start.py*'})
if($projectChildren.Count -ne 1){throw 'Unexpected API children'}
'''
def launch(enabled=True):
    powershell(r'''
$taskRoot=(Get-Location).Path
'''+"$env:RUN_EXECUTOR_ENABLED='"+('true' if enabled else 'false')+"'\n"+r'''
$started=Start-Process -FilePath (Join-Path $taskRoot '.venv\Scripts\python.exe') -ArgumentList 'start.py' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRoot '.local\durable-runs\api-live.out.log') -RedirectStandardError (Join-Path $taskRoot '.local\durable-runs\api-live.err.log') -PassThru
Set-Content -LiteralPath (Join-Path $taskRoot '.local\server.pid') -Value $started.Id -Encoding Ascii
''')
    for _ in range(60):
        try:
            if httpx.get('http://127.0.0.1:8000/health',trust_env=False,timeout=2).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(.5)
    raise TimeoutError('API did not start')
def restart(enabled=True):
    powershell(VERIFY+r'''
Stop-Process -Id $projectChildren[0].ProcessId
Stop-Process -Id $launcherId -ErrorAction SilentlyContinue
''');launch(enabled)

class VerifiedApiHandles:
    """Keep OS handles open BEFORE the fault; PID reuse cannot target another app."""
    def __init__(self):
        if os.name!='nt':raise RuntimeError('This local fault probe requires Windows')
        inventory=json.loads(powershell(VERIFY+r'''
@(@{id=$projectChildren[0].ProcessId;exe=$projectChildren[0].ExecutablePath},@{id=$launcherId;exe=$launcher.ExecutablePath})|ConvertTo-Json -Compress
'''))
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True);self.handles=[]
        self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
        self.kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        for process in inventory:
            handle=self.kernel.OpenProcess(0x100001|0x1000,False,process['id'])
            if not handle:raise RuntimeError('Could not open verified API process')
            buffer=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)) or Path(buffer.value).resolve()!=Path(process['exe']).resolve():
                self.kernel.CloseHandle(handle);raise RuntimeError('API executable changed')
            self.handles.append(handle)
    def kill(self):
        try:
            for handle in self.handles:
                if not self.kernel.TerminateProcess(handle,77):raise RuntimeError('Verified API termination failed')
                self.kernel.WaitForSingleObject(handle,5000)
        finally:self.close()
    def close(self):
        for handle in self.handles:self.kernel.CloseHandle(handle)
        self.handles=[]

async def snapshot():
    db=configured_database();result={}
    try:
        async with db.engine.connect() as c:
            checkpoint_keys=await c.run_sync(lambda conn:{n:inspect(conn).get_pk_constraint(n,schema='smart_cs_checkpoints')['constrained_columns'] for n in ['checkpoint_migrations','checkpoints','checkpoint_blobs','checkpoint_writes']})
        async with db.sessions() as s:
            for name,table in Base.metadata.tables.items():
                if name in {'auth_sessions','auth_login_attempts'}:continue
                keys=','.join('t."'+c.name+'"' for c in table.primary_key)
                result[name]=dict((await s.execute(text(f'SELECT jsonb_build_array({keys})::text,md5(to_jsonb(t)::text) FROM public."{name}" t'))).all())
            for name,columns in checkpoint_keys.items():
                keys=','.join('t."'+c+'"' for c in columns)
                result['checkpoint:'+name]=dict((await s.execute(text(f'SELECT jsonb_build_array({keys})::text,md5(to_jsonb(t)::text) FROM smart_cs_checkpoints."{name}" t'))).all())
        return result
    finally:await db.close()

def state(client,run_id):
    response=client.get('/api/runs/'+run_id);response.raise_for_status();return response.json()
def wait(client,run_id,terminal=True):
    started=time.monotonic();last=None
    while time.monotonic()-started<180:
        try:row=state(client,run_id)
        except httpx.HTTPError:time.sleep(.5);continue
        status=row.get('run_status') or row['status']
        if status!=last:print(json.dumps({'run_id':run_id,'status':status}),flush=True);last=status
        if status not in {'queued','running','cancel_requested'}:return row
        time.sleep(.5)
    raise TimeoutError('Run did not reach a durable terminal status')
def frames(client,run_id,after=0):
    with client.stream('GET','/api/runs/'+run_id+'/events',headers={'Last-Event-ID':str(after)}) as response:
        response.raise_for_status();identifier=None;data=[]
        for line in response.iter_lines():
            if line.startswith('id:'):identifier=int(line[3:].strip())
            elif line.startswith('data:'):data.append(line[5:].lstrip())
            elif not line and data:
                yield {'id':identifier,'data':json.loads('\n'.join(data))};identifier=None;data=[]
def submit(client,suffix,message=MESSAGE):
    body={'client_request_id':'step20-'+suffix+'-'+uuid4().hex,'message':message,'session_id':None}
    response=client.post('/api/runs',json=body);response.raise_for_status()
    return body,response.json()
def assert_replay(client,body,created):
    before=state(client,created['run_id']);events=list(frames(client,created['run_id']))
    for _ in range(2):
        response=client.post('/api/runs',json=body);response.raise_for_status();assert response.json()['run_id']==created['run_id']
    replay=list(frames(client,created['run_id']));after=state(client,created['run_id'])
    assert events==replay and before['model_calls']==after['model_calls']
    assert before['usage']==after['usage']
    ids=[e['id'] for e in events if e['id'] is not None];assert ids==list(range(1,len(ids)+1))
    assert not any(e.get('id') is not None for e in frames(client,created['run_id'],ids[-1]))
    assert sum(e['data']['type'] in {'complete','error'} for e in events)==1
    return {'events':len(ids),'model_calls':len(after['model_calls']),'observed_started_calls':after['usage'].get('observed_started_calls'),'status':after.get('run_status')}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('scenario',choices=['disconnect','queue','crash']);args=parser.parse_args()
    load_dotenv(ENV_PATH);OUT.mkdir(parents=True,exist_ok=True)
    baseline=OUT/'original-rows.json'
    if not baseline.exists():save(baseline.name,asyncio.run(snapshot()))
    report={'passed':False,'scenario':args.scenario,'only_read_tools_requested':True};handles=None
    try:
        if args.scenario=='queue':restart(False)
        if args.scenario=='crash':handles=VerifiedApiHandles()
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=40) as client:
            with authenticated_client(client,'customer-a'):
                body,created=submit(client,args.scenario);report.update(run_id=created['run_id'],session_id=created['session_id'])
                save('live-'+args.scenario+'.json',report)
                if args.scenario=='disconnect':
                    cursor=0
                    for event in frames(client,created['run_id']):
                        cursor=event['id'] or cursor
                        if event['data']['type']=='model_call_started':break
                    assert cursor>0
                    tail=list(frames(client,created['run_id'],cursor));assert any(e['data']['type']=='complete' for e in tail),tail[-1:]
                    report.update(disconnected_after_event=cursor,reconnected_with_last_event_id=True)
                elif args.scenario=='queue':
                    initial=state(client,created['run_id']);assert initial['status']=='queued' and not initial['model_calls']
                    _,cancelled=submit(client,'cancel-queued')
                    response=client.post('/api/runs/'+cancelled['run_id']+'/cancel',json={});response.raise_for_status();assert response.json()['status']=='cancelled'
                    cancelled_state=state(client,cancelled['run_id']);assert not cancelled_state['model_calls'] and cancelled_state['result_event']['code']=='RUN_CANCELLED'
                    report.update(cancelled_queued_run=cancelled['run_id'],queued_cancel_model_calls=0,queued_before_restart=True)
                    restart(True);completed=wait(client,created['run_id']);assert completed['result_event']['type']=='complete'
                    report['queued_run_survived_api_restart']=True
                else:
                    killed=False
                    for event in frames(client,created['run_id']):
                        payload=event['data']
                        if payload['type']=='model_call_started' and payload.get('agent_role')=='compliance_checker':
                            handles.kill();killed=True;break
                    assert killed,'No in-flight compliance model call was observed'
                    launch(True);interrupted=wait(client,created['run_id'])
                    assert interrupted.get('run_status')=='interrupted',interrupted.get('run_status')
                    assert interrupted['usage']['recording_incomplete'] is True
                    old_replay=assert_replay(client,body,created)
                    request={'client_request_id':'step20-explicit-resume-'+uuid4().hex}
                    response=client.post('/api/runs/'+created['run_id']+'/resume',json=request);response.raise_for_status();resumed=response.json()
                    assert resumed['turn_id']==created['turn_id']
                    completed=wait(client,resumed['run_id']);assert completed['result_event']['type']=='complete'
                    again=client.post('/api/runs/'+created['run_id']+'/resume',json=request);again.raise_for_status();assert again.json()['run_id']==resumed['run_id']
                    assert not any(call['agent_role']=='intent_router' for call in completed['model_calls'])
                    report.update(api_killed_during_model_call=True,no_automatic_model_replay=True,interrupted=old_replay,
                        resumed_run_id=resumed['run_id'],same_turn_on_resume=True,intent_router_not_repeated=True)
                    created=resumed
                completed=wait(client,created['run_id']);assert completed['result_event']['type']=='complete'
                report['replay']=assert_replay(client,body,created) if args.scenario!='crash' else {'status':completed['run_status'],'model_calls':len(completed['model_calls'])}
                history=client.get('/api/history/'+created['session_id']);history.raise_for_status();messages=history.json()['messages']
                assert [m['role'] for m in messages]==['user','assistant']
                report.update(final_run_id=created['run_id'],canonical_message_count=len(messages),idempotent_submission=True)
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=20) as foreign:
            with authenticated_client(foreign,'customer-b'):
                for path in ['/api/runs/'+report['run_id'],'/api/runs/'+report['run_id']+'/events','/api/runs/by-request/'+body['client_request_id']]:assert foreign.get(path).status_code==404
        report.update(passed=True,foreign_account_denied=True)
    except Exception as error:report.update(error_type=type(error).__name__)
    finally:
        if handles:handles.close()
        if args.scenario=='queue' and not report['passed']:
            try:restart(True)
            except Exception:pass
    save('live-'+args.scenario+'.json',report);print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
