"""One-time step-ten maintenance: verified backup, preserving every existing row."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
import subprocess
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.persistence.postgres import prepare_checkpoint_store
from verify_core_schema import fingerprints


async def snapshot():
    db=configured_database()
    try:
        async with db.sessions() as s:revision=await s.scalar(text('SELECT version_num FROM public.alembic_version'))
        return {'revision':revision,'tables':await fingerprints(db,legacy_conversation_projection=True)}
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH)
    output=PROJECT_ROOT/'.local/conversations';output.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'stage':'snapshot'}
    try:
        before=asyncio.run(snapshot())
        if before['revision']!='0007_ticket_service':raise RuntimeError('Expected pre-step-ten schema')
        compose=['docker','compose','-p','smart-cs-foundation','-f',str(PROJECT_ROOT/'docker-compose.yml'),'exec','-T','postgres']
        result=subprocess.run(compose+['pg_dump','-U',os.getenv('POSTGRES_USER','smart_cs'),'-d',os.getenv('POSTGRES_DB','smart_cs'),'-Fc'],capture_output=True,timeout=60)
        if result.returncode or not result.stdout.startswith(b'PGDMP'):raise RuntimeError('Backup failed')
        listing=subprocess.run(compose+['pg_restore','--list'],input=result.stdout,capture_output=True,timeout=30)
        if listing.returncode:raise RuntimeError('Backup catalogue failed')
        folder=PROJECT_ROOT/'.local/backups'/('pre-step-ten-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));folder.mkdir(parents=True)
        backup=folder/'database.dump';backup.write_bytes(result.stdout)
        manifest={'sha256':hashlib.sha256(result.stdout).hexdigest(),'bytes':len(result.stdout),'catalogue_valid':True,'before':before}
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        report.update(backup=str(backup),backup_sha256=manifest['sha256'],stage='migration')
        config=Config(str(PROJECT_ROOT/'alembic.ini'));command.upgrade(config,'head');command.check(config)
        async def prepare():
            db=configured_database()
            try:return await prepare_checkpoint_store(db)
            finally:await db.close()
        checkpoints=asyncio.run(prepare());after=asyncio.run(snapshot())
        if before['tables']!=after['tables']:raise AssertionError('Pre-existing business rows changed')
        report.update(passed=True,stage='complete',revision=after['revision'],preexisting_rows_preserved=True,
            old_sessions_not_fabricated=True,checkpoints=checkpoints,before=before,after=after)
    except Exception as error:report['error_type']=type(error).__name__
    (output/'live-upgrade.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'before','after'}},indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
