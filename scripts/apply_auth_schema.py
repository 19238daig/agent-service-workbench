"""Back up before auth migration; preserve pre-existing facts and corpus/index bytes."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text, select, func, or_
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import KnowledgeDocument
from verify_core_schema import fingerprints


async def snapshot():
    db=configured_database()
    try:
        async with db.sessions() as session:revision=await session.scalar(text('SELECT version_num FROM alembic_version'))
        return {'revision':revision,'tables':await fingerprints(db)}
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH)
    report={'passed':False,'stage':'snapshot'}
    output=PROJECT_ROOT/'.local/auth';output.mkdir(parents=True,exist_ok=True)
    try:
        before=asyncio.run(snapshot());report['before']=before
        if before['revision']!='0004_core_sessions':raise RuntimeError('Expected pre-auth schema; refusing to rerun maintenance')
        report['stage']='backup'
        compose=['docker','compose','-p','smart-cs-foundation','-f',str(PROJECT_ROOT/'docker-compose.yml'),'exec','-T','postgres']
        result=subprocess.run(compose+['pg_dump','-U',os.environ.get('POSTGRES_USER','smart_cs'),'-d',os.environ.get('POSTGRES_DB','smart_cs'),'-Fc'],
            cwd=PROJECT_ROOT,capture_output=True,timeout=60)
        if result.returncode or not result.stdout.startswith(b'PGDMP'):raise RuntimeError('Backup failed')
        folder=PROJECT_ROOT/'.local/backups'/('pre-step-seven-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        folder.mkdir(parents=True)
        backup=folder/'database.dump';backup.write_bytes(result.stdout)
        listing=subprocess.run(compose+['pg_restore','--list'],input=result.stdout,capture_output=True,timeout=30)
        if listing.returncode:raise RuntimeError('Backup catalogue validation failed')
        manifest={'sha256':hashlib.sha256(result.stdout).hexdigest(),'bytes':len(result.stdout),'format':'PostgreSQL custom',
                  'catalogue_valid':True,'before':before}
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        report.update(backup=str(backup),backup_sha256=manifest['sha256'],stage='upgrade')
        config=Config(str(PROJECT_ROOT/'alembic.ini'));command.upgrade(config,'head');command.check(config)
        after=asyncio.run(snapshot());report['after']=after
        if not all(after['tables'].get(name)==value for name,value in before['tables'].items()):raise AssertionError('Pre-existing facts changed')
        async def public_defaults():
            db=configured_database()
            try:
                async with db.sessions() as session:
                    return await session.scalar(select(func.count()).select_from(KnowledgeDocument).where(or_(KnowledgeDocument.access_scope!='public',KnowledgeDocument.owner_id.is_not(None))))==0
            finally:await db.close()
        report.update(passed=True,stage='complete',preexisting_rows_preserved=True,existing_documents_public=asyncio.run(public_defaults()))
    except Exception as error:report['error_type']=type(error).__name__
    (output/'live-upgrade.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key not in {'before','after'}},indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
