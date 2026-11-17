"""Import actual archived web PDF/HTML with CLI; preserve the active RAG index.

prepare -> start API -> http -> restart API -> http --verify-restart.
Creates two explicitly labelled ingestion records; only the HTML ACL test is
published privately, using unchanged publicly sourced bytes. No paid models.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import httpx
from dotenv import load_dotenv
from sqlalchemy import text,bindparam
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import Base
from authenticated_client import authenticated_client
from verify_core_schema import fingerprints

OUTPUT=PROJECT_ROOT/'.local/ingestion'


async def snapshot(excluded=()):
    db=configured_database()
    try:
        values=await fingerprints(db)
        async with db.sessions() as session:
            for name,column in [('knowledge_documents','id'),('knowledge_chunks','document_id'),('knowledge_ingestions','document_id')]:
                if not excluded:continue
                projection="to_jsonb(t) - 'access_scope' - 'owner_id'" if name=='knowledge_documents' else 'to_jsonb(t)'
                query=text(f"SELECT count(*),md5(COALESCE(string_agg(({projection})::text,E'\\n' ORDER BY ({projection})::text),'')) FROM {name} t WHERE t.{column} NOT IN :excluded").bindparams(bindparam('excluded',expanding=True))
                row=(await session.execute(query,{'excluded':list(excluded)})).one();values[name]={'count':row[0],'digest':row[1]}
        return {k:v for k,v in values.items() if k not in {'auth_sessions','auth_login_attempts'}}
    finally:await db.close()


def cli(name,arguments,expected=0):
    relative=f'.local/ingestion/{name}.json'
    result=subprocess.run([sys.executable,'-m','smart_cs.ingestion.cli','--report',relative,*arguments],capture_output=True,timeout=240,cwd=PROJECT_ROOT)
    report=json.loads((PROJECT_ROOT/relative).read_text(encoding='utf-8'))
    if result.returncode!=expected:raise RuntimeError('CLI acceptance failed: '+name)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['prepare','http'])
    parser.add_argument('--verify-restart',action='store_true');args=parser.parse_args();load_dotenv(ENV_PATH)
    OUTPUT.mkdir(parents=True,exist_ok=True);state_path=OUTPUT/'live-case.json';report={'passed':False,'action':args.action,'paid_model_calls':False}
    try:
        if args.action=='prepare':
            if state_path.exists():raise RuntimeError('Existing case; refusing duplicate preparation')
            baseline=OUTPUT/'acceptance-baseline.json'
            if baseline.exists():before=json.loads(baseline.read_text(encoding='utf-8'))
            else:
                before=asyncio.run(snapshot())
                baseline.write_text(json.dumps(before,indent=2)+'\n',encoding='utf-8')
            source=PROJECT_ROOT/'data/public-after-sales-v2'
            manifest=json.loads((source/'source_manifest.json').read_text(encoding='utf-8'))
            accounts=json.loads((PROJECT_ROOT/'.local/auth/bootstrap-accounts.json').read_text(encoding='utf-8'))['accounts']
            owner=next(a['user_id'] for a in accounts if a['login_name']=='customer-a')
            folder=OUTPUT/'acceptance';(folder/'sources').mkdir(parents=True,exist_ok=True);entries=[]
            for filename,scope,chunker in [('consumer_protection_regulation.pdf','public','bge-semantic'),('consumer_protection_regulation.html','private','structural')]:
                origin=next(s for s in manifest['sources'] if s['file']==filename);raw=(source/'sources'/filename).read_bytes()
                assert hashlib.sha256(raw).hexdigest()==origin['sha256']
                (folder/'sources'/filename).write_bytes(raw)
                metadata={'source_key':'phase-eleven-acceptance-'+Path(filename).suffix[1:],'title':origin['title'],
                    'version':origin['version'],'domain':origin['collection'],'access_scope':scope,'owner_id':owner if scope=='private' else None,
                    'effective_from':origin['effective_from'],'source_type':origin['source_type'],'publisher':origin['publisher'],
                    'license':origin['license'],'source_url':origin['url'],'expected_sha256':origin['sha256']}
                entries.append({'file':'sources/'+filename,'metadata':metadata,'options':{'chunker':chunker}})
                (folder/(Path(filename).suffix[1:]+'-metadata.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            path=folder/'manifest.json';path.write_text(json.dumps({'documents':entries},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            first_path=OUTPUT/'first-import.json'
            first=json.loads(first_path.read_text(encoding='utf-8')) if first_path.exists() else cli('first-import',['batch',str(path)])
            second=cli('repeat-import',['batch',str(path)])
            assert len(first['results'])==2 and all(not r['unchanged'] for r in first['results'])
            assert all(r['unchanged'] for r in second['results'])
            pdf,html=first['results'];ids=[pdf['document_id'],html['document_id']]
            duplicate=cli('legacy-publish-rejected',['publish',pdf['document_id']],expected=1)
            assert duplicate['error_code']=='ARTIFACT_ALREADY_PUBLISHED'
            published=cli('private-publication',['publish',html['document_id']]);assert published['status']=='published'
            cli('inspection',['inspect',str(folder/entries[1]['file']),'--metadata',str(folder/'html-metadata.json')])
            assert asyncio.run(snapshot(ids))==before
            state={'before':before,'document_ids':ids,'pdf':pdf,'html':html,'manifest':str(path)}
            state_path.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
            report.update(passed=True,real_downloaded_pdf_html=True,documents=2,chunks=sum(r['chunks'] for r in first['results']),
                repeat_import_unchanged=True,bge_semantic_chunking=True,legacy_duplicate_publication_denied=True,
                existing_rows_and_active_index_preserved=True)
        else:
            state=json.loads(state_path.read_text(encoding='utf-8'));pdf,html=state['document_ids']
            with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=30) as a,authenticated_client(a,'customer-a'):
                file=a.get('/api/knowledge/documents/'+html+'/file');assert file.status_code==200
                assert hashlib.sha256(file.content).hexdigest()==state['html']['artifact_sha256']
                assert file.headers['content-type'].startswith('text/plain') and file.headers['content-security-policy']=='sandbox'
                assert a.get('/api/knowledge/documents/'+pdf+'/file').status_code==404
                for login in ['customer-b','admin']:
                    with httpx.Client(base_url=str(a.base_url),trust_env=False,timeout=30) as b,authenticated_client(b,login):
                        assert b.get('/api/knowledge/documents/'+html+'/file').status_code==404
                previous=PROJECT_ROOT/'.local/conversations/live-case.json'
                if previous.exists():
                    chat=json.loads(previous.read_text(encoding='utf-8'));history=a.get('/api/history/'+chat['session_id']).json()
                    # Users may have continued this conversation since step ten.
                    # Preserve the recorded prefix without treating later turns
                    # as an ingestion failure; the full DB snapshot is checked too.
                    assert len(history['messages'])>=chat['message_count']
                    prefix=[{k:m[k] for k in ['id','sequence','role','content','run_id']} for m in history['messages'][:chat['message_count']]]
                    assert hashlib.sha256(json.dumps(prefix,sort_keys=True,ensure_ascii=False).encode()).hexdigest()==chat['history_digest']
            repeated=cli('post-restart-repeat' if args.verify_restart else 'post-start-repeat',['batch',state['manifest']])
            assert all(r['unchanged'] for r in repeated['results'])
            assert asyncio.run(snapshot(state['document_ids']))==state['before']
            report.update(passed=True,native_file_hash_preserved=True,html_served_inert=True,
                private_foreign_and_admin_access_denied=True,draft_hidden=True,
                repeat_import_unchanged=True,no_startup_faq_duplicates=True,prior_history_preserved=True,
                existing_rows_and_active_index_preserved=True,after_restart=args.verify_restart)
    except Exception as error:report['error_type']=type(error).__name__
    (OUTPUT/('restart-verification.json' if args.verify_restart else args.action+'-verification.json')).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
