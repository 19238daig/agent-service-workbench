"""Checksummed database + data backup. Restore only into a new drill database."""
import asyncio,hashlib,json,os,subprocess,tarfile
from pathlib import Path
from sqlalchemy import text,inspect
from smart_cs.settings import PROJECT_ROOT
from smart_cs.db.database import Database


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def rows(url):
    db=Database(url);result={}
    try:
        async with db.engine.connect() as c:
            names=await c.run_sync(lambda conn:{s:inspect(conn).get_table_names(schema=s) for s in ['public','smart_cs_checkpoints']})
        async with db.sessions() as s:
            for schema,tables in names.items():
                for table in tables:
                    # Names come from the database catalog, quote them as identifiers.
                    qualified='"'+schema.replace('"','""')+'"."'+table.replace('"','""')+'"'
                    statement=f"SELECT count(*),md5(COALESCE(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM {qualified} t"
                    row=(await s.execute(text(statement))).one();result[schema+'.'+table]={'rows':row[0],'digest':row[1]}
        return result
    finally:await db.close()


def inventory(root):
    root=Path(root).resolve();result={}
    for base in [root/'data',root/'.local/background-jobs']:
        for p in sorted(base.rglob('*')):
            if p.is_symlink():raise ValueError('Symbolic link in backup source')
            if p.is_file():
                if not p.resolve().is_relative_to(base):raise ValueError('Source outside backup directory')
                result[p.relative_to(root).as_posix()]={'sha256':sha(p),'bytes':p.stat().st_size}
    return result


def export(folder,url,compose,source_root=PROJECT_ROOT):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    before=asyncio.run(rows(url));files=inventory(source_root)
    with (folder/'database.dump').open('wb') as stream:
        result=subprocess.run([*compose,'exec','-T','postgres','pg_dump','-U','smart_cs','-d','smart_cs',
            '--format=custom','--no-owner','--no-acl','--exclude-schema=foundation_verify_*'],stdout=stream,stderr=subprocess.PIPE)
    if result.returncode:raise RuntimeError('Database backup failed; raw driver output suppressed')
    with tarfile.open(folder/'attachments.tar','w') as archive:
        for name in files:archive.add(Path(source_root)/name,arcname=name,recursive=False)
    after=asyncio.run(rows(url));assert before==after,'Concurrent source database change during snapshot verification'
    assert files==inventory(source_root),'Concurrent source file change during backup'
    manifest={'format':'smart-cs-backup-v1','database_sha256':sha(folder/'database.dump'),
        'attachments_sha256':sha(folder/'attachments.tar'),'tables':before,'files':files,
        'database_dump_transaction_consistent':True,'database_and_files_observed_unchanged_during_backup':True,
        'redis_is_disposable_cache_or_delivery_transport':True,'queue_authority':'PostgreSQL background_jobs/run_controls; do not restore Redis caches',
        'includes_credentials_hashes_and_private_data':True,'model_env_included':False}
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest


def validate(folder):
    folder=Path(folder);manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    assert sha(folder/'database.dump')==manifest['database_sha256']
    assert sha(folder/'attachments.tar')==manifest['attachments_sha256']
    return manifest
