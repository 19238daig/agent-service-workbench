"""Back up and apply step-six migration without changing existing business facts."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.seed import seed_database
from verify_core_schema import fingerprints


def snapshot():
    async def execute():
        database=configured_database()
        try:
            async with database.sessions() as session:revision=await session.scalar(text('SELECT version_num FROM alembic_version'))
            return {'revision':revision,'tables':await fingerprints(database)}
        finally:await database.close()
    return asyncio.run(execute())


def main():
    load_dotenv(ENV_PATH)
    output=PROJECT_ROOT/'.local/database';output.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'stage':'snapshot','date':'2026-11-14'}
    try:
        before=snapshot();report['before']=before
        report['stage']='backup'
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        folder=PROJECT_ROOT/'.local/backups'/('pre-step-six-'+stamp);folder.mkdir(parents=True)
        result=subprocess.run(['docker','compose','-p','smart-cs-foundation','-f',str(PROJECT_ROOT/'docker-compose.yml'),
            'exec','-T','postgres','pg_dump','-U',os.environ.get('POSTGRES_USER','smart_cs'),'-d',os.environ.get('POSTGRES_DB','smart_cs'),'-Fc'],
            cwd=PROJECT_ROOT,capture_output=True,timeout=60)
        if result.returncode or not result.stdout.startswith(b'PGDMP'):raise RuntimeError('Database backup did not complete')
        backup=folder/'database.dump';backup.write_bytes(result.stdout)
        manifest={'created_at':datetime.now(timezone.utc).isoformat(),'format':'PostgreSQL custom','sha256':hashlib.sha256(result.stdout).hexdigest(),'bytes':len(result.stdout),'before':before,'credentials_included':False}
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        report['backup']=str(backup);report['backup_sha256']=manifest['sha256']
        report['stage']='upgrade'
        config=Config(str(PROJECT_ROOT/'alembic.ini'));command.upgrade(config,'head')
        upgraded=snapshot()
        if not all(upgraded['tables'].get(name)==value for name,value in before['tables'].items()):raise AssertionError('Existing rows changed during migration')
        report['stage']='seed'
        async def seed():
            database=configured_database()
            try:
                first=await seed_database(database);second=await seed_database(database)
                if first['database']['counts']!=second['database']['counts']:raise AssertionError('Seed changed on second execution')
                return second['database']
            finally:await database.close()
        report['database']=asyncio.run(seed())
        after=snapshot()
        if not all(after['tables'].get(name)==value for name,value in before['tables'].items()):raise AssertionError('Existing rows changed while seeding new session fixtures')
        command.check(config)
        report.update(passed=True,stage='complete',after=after,preexisting_rows_unchanged=True,seed_idempotent=True)
    except Exception as error:
        report['error_type']=type(error).__name__
        # Do not restore automatically or expose SQL parameters/connection strings.
    (output/'live-upgrade.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'before','after'}},ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
