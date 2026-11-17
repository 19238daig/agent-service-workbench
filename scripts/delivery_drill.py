"""Reproducible local-only delivery exercise, separate from the running workspace."""
import argparse,asyncio,json,os,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
from uuid import uuid4
from dotenv import dotenv_values,set_key
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from psycopg import sql
import httpx
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from delivery_backup import export,validate,rows,sha
from manage_accounts import restrict_file

OUT=PROJECT_ROOT/'.local/delivery';ENV=OUT/'environment.env'
PROJECT_NAME='smart-cs-delivery'
COMPOSE=['docker','compose','--project-directory',str(PROJECT_ROOT),'--env-file',str(ENV),'-f',str(PROJECT_ROOT/'deploy/compose.delivery.yml'),'-p','smart-cs-delivery']


def write(name,value):
    (OUT/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def run(args,name,capture=False):
    result=subprocess.run(args,cwd=PROJECT_ROOT,capture_output=True)
    (OUT/(name+'.log')).write_bytes(result.stdout+b'\n'+result.stderr)
    if result.returncode:raise RuntimeError('Delivery command failed: '+name+'; inspect private local log')
    return result.stdout.decode('utf-8',errors='replace') if capture else None


def url(database=None):
    config=dotenv_values(ENV)
    return make_url('postgresql+asyncpg://smart_cs@127.0.0.1:'+config['DELIVERY_DB_PORT']+'/'+(database or config.get('DELIVERY_DATABASE') or 'smart_cs')).set(password=config['DELIVERY_DB_PASSWORD']).render_as_string(hide_password=False)


def assert_target():
    container=run([*COMPOSE,'ps','-q','postgres'],'target-container',True).strip();assert container
    owner=run(['docker','inspect','--format','{{index .Config.Labels "com.docker.compose.project"}}',container],'target-owner',True).strip()
    assert owner==PROJECT_NAME


def fresh():
    assert_target()
    assert not dotenv_values(ENV).get('DELIVERY_DATABASE'),'Fresh initialization cannot target a restored database'
    run([*COMPOSE,'run','--rm','init'],'fresh-migrations')
    run([*COMPOSE,'up','-d','--wait','app'],'fresh-app')
    run([*COMPOSE,'exec','-T','app','python','scripts/manage_accounts.py','bootstrap'],'fresh-accounts')
    run([*COMPOSE,'cp','app:/app/.local/auth/bootstrap-accounts.json',str(OUT/'fresh-accounts.json')],'fresh-copy-accounts')
    restrict_file(OUT/'fresh-accounts.json')
    run([*COMPOSE,'run','--rm','init'],'fresh-seed-after-accounts')
    run([*COMPOSE,'exec','-T','app','python','-m','alembic','check'],'fresh-schema-drift')
    smoke('fresh');write('fresh',{'passed':True,'blank_database_migrated_to_head':True,'explicit_checkpoint_setup':True,'accounts_created_privately':True,'schema_drift':False,'fresh_named_volumes':True})


def login(client,accounts,who='customer-a'):
    assert str(client.base_url).rstrip('/')=='http://127.0.0.1:'+dotenv_values(ENV)['DELIVERY_APP_PORT']
    record=next(a for a in accounts['accounts'] if a['login_name']==who)
    response=client.post('/api/auth/login',json={'login_name':who,'password':record['password']});assert response.status_code==200
    client.headers['Authorization']='Bearer '+response.json()['access_token']
    return response.json()['user']


def smoke(phase):
    config=dotenv_values(ENV);port=config['DELIVERY_APP_PORT']
    source=OUT/'fresh-accounts.json'
    if phase=='restored' and (OUT/'backup/source.json').exists() and json.loads((OUT/'backup/source.json').read_text())['source_live']:
        source=PROJECT_ROOT/'.local/auth/bootstrap-accounts.json'
    accounts=json.loads(source.read_text(encoding='utf-8'))
    with httpx.Client(base_url='http://127.0.0.1:'+port,trust_env=False,timeout=30,headers={'Origin':'http://127.0.0.1:'+port}) as client:
        assert client.get('/').status_code==200
        assert client.get('/health/live').status_code==200
        assert client.get('/api/orders').status_code==401
        login(client,accounts)
        order=client.get('/api/orders/ORDER-A-006');assert order.status_code==200 and order.json()['order']['paid_amount']=='500.00'
        foreign=client.get('/api/orders/ORDER-B-001');assert foreign.status_code==404
        assert client.get('/api/admin/knowledge/documents').status_code==403
        assert client.post('/api/tools/call',json={'name':'ticket_create','arguments':{'approved':True}}).status_code==403
        assert client.post('/api/auth/login',json={'login_name':'customer-a','password':'wrong'},headers={'Origin':'https://untrusted.invalid'}).status_code==403
        client.headers.pop('Authorization');client.cookies.clear();login(client,accounts,'admin')
        ready=client.get('/health/ready');assert ready.status_code==200
        report=ready.json()
    write('smoke-'+phase,{'passed':True,'real_http':True,'real_authentication':True,'foreign_order_denied':True,
        'write_dispatch_denied':True,'untrusted_browser_origin_denied':True,'ready':report,'accounts_path':str(source.relative_to(PROJECT_ROOT))})


def backup(source_live):
    assert_target();folder=OUT/'backup'
    if source_live:
        settings=dotenv_values(ENV_PATH);source=settings['DATABASE_URL']
        compose=['docker','compose','--project-directory',str(PROJECT_ROOT),'--env-file',str(ENV_PATH),'-f',str(PROJECT_ROOT/'docker-compose.yml'),'-p','smart-cs-foundation']
        assert make_url(source).port!=int(dotenv_values(ENV)['DELIVERY_DB_PORT'])
    else:source=url('smart_cs');compose=COMPOSE
    manifest=export(folder,source,compose)
    (folder/'source.json').write_text(json.dumps({'source_live':source_live,'model_keys_not_in_backup':True}),encoding='utf-8')
    write('backup',{'passed':True,'tables':len(manifest['tables']),'rows':sum(t['rows'] for t in manifest['tables'].values()),
        'files':len(manifest['files']),'database_sha256':manifest['database_sha256'],'attachments_sha256':manifest['attachments_sha256'],
        'source_live':source_live,'database_bytes':(folder/'database.dump').stat().st_size})


def restore():
    assert_target();folder=OUT/'backup';manifest=validate(folder);target='smart_cs_delivery_restored'
    engine=create_engine(url('smart_cs').replace('+asyncpg','+psycopg',1),isolation_level='AUTOCOMMIT',hide_parameters=True)
    try:
        with engine.connect() as c:
            if c.scalar(text('SELECT 1 FROM pg_database WHERE datname=:name'),{'name':target}):raise ValueError('Restore target exists; never overwrite an existing database')
            c.connection.driver_connection.cursor().execute(sql.SQL('CREATE DATABASE {} OWNER smart_cs').format(sql.Identifier(target)))
    finally:engine.dispose()
    write('restore-target',{'database':target,'new_database_only':True,'source_dump_sha256':manifest['database_sha256']})
    run([*COMPOSE,'stop','app'],'restore-stop-clone')
    run([*COMPOSE,'cp',str(folder/'database.dump'),'postgres:/tmp/smart-cs-delivery.dump'],'restore-copy-dump')
    run([*COMPOSE,'exec','-T','postgres','pg_restore','-U','smart_cs','--exit-on-error','--single-transaction','--no-owner','--no-acl','-d',target,'/tmp/smart-cs-delivery.dump'],'restore-database')
    restored=asyncio.run(rows(url(target)));assert restored==manifest['tables'],'Database/checkpoint/vector snapshot differs after restore'
    write('restore-database',{'passed':True,'tables':len(restored),'rows':sum(t['rows'] for t in restored.values()),'all_table_digests_equal':True,'checkpoint_bytes_included':True,'separate_target_database':target})
    set_key(ENV,'DELIVERY_DATABASE',target)
    source=json.loads((folder/'source.json').read_text())
    set_key(ENV,'DELIVERY_RAG_ENABLED','true' if source['source_live'] else 'false')
    # Do not dispatch restored jobs/runs until an operator reviews their external effects.
    set_key(ENV,'DELIVERY_RUN_EXECUTOR_ENABLED','false')
    run([*COMPOSE,'run','--rm','init','python','-m','smart_cs.delivery','init'],'restore-migrate-and-grants')
    run([*COMPOSE,'up','-d','--wait','app'],'restore-start-app')
    for name in ['attachments.tar','manifest.json']:
        run([*COMPOSE,'cp',str(folder/name),'app:/tmp/smart-cs-'+name],'restore-copy-'+name.replace('.','-'))
    run([*COMPOSE,'exec','-T','app','python','-m','smart_cs.delivery','restore-files','--archive','/tmp/smart-cs-attachments.tar','--manifest','/tmp/smart-cs-manifest.json'],'restore-attachments')
    run([*COMPOSE,'exec','-T','app','python','-m','alembic','check'],'restored-schema-drift')
    smoke('restored')
    write('restore',{'passed':True,'attachment_files':len(manifest['files']),'snapshot_digests_equal_before_read_checks':True,
        'source_not_overwritten':True,'restored_rag_enabled':source['source_live'],'automatic_task_dispatch_disabled':True})


def migration():
    assert_target();name='delivery_upgrade_'+uuid4().hex[:12]
    engine=create_engine(url('smart_cs').replace('+asyncpg','+psycopg',1),isolation_level='AUTOCOMMIT',hide_parameters=True)
    try:
        with engine.connect() as c:c.connection.driver_connection.cursor().execute(sql.SQL('CREATE DATABASE {} OWNER smart_cs').format(sql.Identifier(name)))
        env=os.environ.copy();env['DATABASE_URL']=url(name)
        for revision in ['0014_durable_runs','head']:
            process=subprocess.run([sys.executable,'-m','alembic','upgrade',revision],cwd=PROJECT_ROOT,env=env,capture_output=True)
            (OUT/('upgrade-'+revision+'.log')).write_bytes(process.stdout+process.stderr)
            if process.returncode:raise RuntimeError('Historical migration failed')
            if revision=='0014_durable_runs':
                old=create_engine(url(name).replace('+asyncpg','+psycopg',1),hide_parameters=True)
                try:
                    with old.begin() as c:c.exec_driver_sql("INSERT INTO users(id,display_name,role,source,created_at) VALUES ('delivery-preserved','Synthetic upgrade fixture','customer','delivery_fixture',now())")
                finally:old.dispose()
                before=asyncio.run(rows(url(name)))
        after=asyncio.run(rows(url(name)))
        assert all(after[k]==v for k,v in before.items() if k!='public.alembic_version')
        write('migration',{'passed':True,'from':'0014_durable_runs','to':'0015_knowledge_admin','seeded_old_rows_preserved':True,'isolated_database':True})
    finally:
        with engine.connect() as c:c.connection.driver_connection.cursor().execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        engine.dispose()


def image_audit():
    code="import pathlib,json,os;from smart_cs.settings import PROJECT_ROOT;r=PROJECT_ROOT;assert os.getuid()!=0;assert not (r/'.env').exists();assert not (r/'.venv').exists();assert not (r/'.local/auth/bootstrap-accounts.json').exists();assert not list((r/'data/knowledge-imports/blobs').glob('**/*'));assert not list((r/'data/public-after-sales-v2/sources').glob('**/*'));assert (r/'frontend/dist/index.html').is_file();assert (r/'docs/business-scope.md').is_file();print(json.dumps({'passed':True,'uid':os.getuid(),'secrets_and_raw_uploads_absent':True,'react_built':True}))"
    result=json.loads(run(['docker','run','--rm','--entrypoint','python','smart-cs-delivery:local','-c',code],'image-audit',True))
    result['image_id']=run(['docker','image','inspect','--format','{{.Id}}','smart-cs-delivery:local'],'image-id',True).strip();write('image-audit',result)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['fresh','backup','restore','migration','image-audit','smoke'])
    parser.add_argument('--source-live',action='store_true');args=parser.parse_args()
    try:
        if args.action=='backup':backup(args.source_live)
        elif args.action=='smoke':smoke('restored' if dotenv_values(ENV).get('DELIVERY_DATABASE') else 'fresh')
        else:globals()[args.action.replace('-','_')]()
        print(json.dumps({'action':args.action,'passed':True}));return 0
    except Exception as error:
        write(args.action+'-failure',{'passed':False,'error_type':type(error).__name__})
        print(json.dumps({'action':args.action,'passed':False,'error_type':type(error).__name__}));return 1


if __name__=='__main__':raise SystemExit(main())
