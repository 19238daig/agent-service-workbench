"""Back up, migrate without rewriting history, then insert synthetic ticket cases."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
import subprocess
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import select,func,or_,text
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import Ticket,TicketEvent
from smart_cs.ticket_fixtures import seed_ticket_scenarios,FIXTURES
from verify_core_schema import fingerprints


async def snapshot(exclude=None):
    db=configured_database()
    try:
        async with db.sessions() as session:revision=await session.scalar(text('SELECT version_num FROM public.alembic_version'))
        return {'revision':revision,'tables':await fingerprints(db,legacy_ticket_projection=True,exclude_ticket_ids=exclude)}
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH)
    output=PROJECT_ROOT/'.local/tickets';output.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'stage':'snapshot'}
    try:
        before=asyncio.run(snapshot());report['before']=before
        if before['revision']!='0006_order_facts':raise RuntimeError('Expected pre-ticket-service revision; maintenance is not repeatable')
        report['stage']='backup'
        compose=['docker','compose','-p','smart-cs-foundation','-f',str(PROJECT_ROOT/'docker-compose.yml'),'exec','-T','postgres']
        result=subprocess.run(compose+['pg_dump','-U',os.getenv('POSTGRES_USER','smart_cs'),'-d',os.getenv('POSTGRES_DB','smart_cs'),'-Fc'],capture_output=True,cwd=PROJECT_ROOT,timeout=60)
        if result.returncode or not result.stdout.startswith(b'PGDMP'):raise RuntimeError('Backup failed')
        folder=PROJECT_ROOT/'.local/backups'/('pre-step-nine-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));folder.mkdir(parents=True)
        backup=folder/'database.dump';backup.write_bytes(result.stdout)
        listing=subprocess.run(compose+['pg_restore','--list'],input=result.stdout,capture_output=True,timeout=30)
        if listing.returncode:raise RuntimeError('Backup catalogue failed')
        manifest={'sha256':hashlib.sha256(result.stdout).hexdigest(),'bytes':len(result.stdout),'catalogue_valid':True,'before':before}
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        report.update(backup=str(backup),backup_sha256=manifest['sha256'],stage='migration')
        config=Config(str(PROJECT_ROOT/'alembic.ini'));command.upgrade(config,'head');command.check(config)
        after=asyncio.run(snapshot())
        if before['tables']!=after['tables']:raise AssertionError('Pre-existing rows changed')
        async def check_and_seed():
            db=configured_database()
            try:
                async with db.sessions() as session:
                    assigned=await session.scalar(select(func.count()).select_from(Ticket).where(Ticket.assignee_id.is_not(None)))
                    filled=await session.scalar(select(func.count()).select_from(TicketEvent).where(or_(
                        TicketEvent.ticket_version.is_not(None),TicketEvent.actor_role.is_not(None),TicketEvent.source.is_not(None),
                        TicketEvent.assignee_before.is_not(None),TicketEvent.assignee_after.is_not(None),TicketEvent.note!='')))
                if assigned or filled:raise AssertionError('Historical assignment or audit facts were inferred')
                first=await seed_ticket_scenarios(db);second=await seed_ticket_scenarios(db)
                if first['inserted']!=3 or second['inserted']!=0 or second['retained']!=3:raise AssertionError('Unexpected ticket seed result')
                return first,second,await db.summary()
            finally:await db.close()
        report['stage']='seed'
        first,second,summary=asyncio.run(check_and_seed())
        preserved=asyncio.run(snapshot([row[0] for row in FIXTURES]))
        if preserved['tables']!=before['tables']:
            raise AssertionError('Existing records changed after inserting synthetic timelines')
        report.update(passed=True,stage='complete',revision=after['revision'],preexisting_rows_preserved=True,
            legacy_audit_remains_unknown=True,first_seed=first,second_seed=second,seed_idempotent=True,database=summary)
    except Exception as error:report['error_type']=type(error).__name__
    (output/'live-upgrade.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='before'},indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
