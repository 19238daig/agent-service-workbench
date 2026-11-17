"""Maintenance: verified backup, additive migration and insert-only order scenarios."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
import subprocess
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text,select,func,or_
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import Order
from smart_cs.order_fixtures import seed_order_scenarios,load_order_scenarios
from verify_core_schema import fingerprints


async def snapshot(exclude=None):
    db=configured_database()
    try:
        async with db.sessions() as session:revision=await session.scalar(text('SELECT version_num FROM public.alembic_version'))
        return {'revision':revision,'tables':await fingerprints(db,legacy_order_projection=True,exclude_order_ids=exclude)}
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH)
    output=PROJECT_ROOT/'.local/orders';output.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'stage':'snapshot'}
    try:
        before=asyncio.run(snapshot());report['before']=before
        if before['revision']!='0005_auth_identity':raise RuntimeError('Expected pre-order-facts revision; refusing to rerun maintenance')
        report['stage']='backup'
        compose=['docker','compose','-p','smart-cs-foundation','-f',str(PROJECT_ROOT/'docker-compose.yml'),'exec','-T','postgres']
        result=subprocess.run(compose+['pg_dump','-U',os.getenv('POSTGRES_USER','smart_cs'),'-d',os.getenv('POSTGRES_DB','smart_cs'),'-Fc'],capture_output=True,cwd=PROJECT_ROOT,timeout=60)
        if result.returncode or not result.stdout.startswith(b'PGDMP'):raise RuntimeError('Backup failed')
        folder=PROJECT_ROOT/'.local/backups'/('pre-step-eight-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));folder.mkdir(parents=True)
        backup=folder/'database.dump';backup.write_bytes(result.stdout)
        listing=subprocess.run(compose+['pg_restore','--list'],input=result.stdout,capture_output=True,timeout=30)
        if listing.returncode:raise RuntimeError('Backup catalogue failed')
        manifest={'sha256':hashlib.sha256(result.stdout).hexdigest(),'bytes':len(result.stdout),'catalogue_valid':True,'before':before}
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        report.update(backup=str(backup),backup_sha256=manifest['sha256'],stage='migration')
        config=Config(str(PROJECT_ROOT/'alembic.ini'));command.upgrade(config,'head');command.check(config)
        after=asyncio.run(snapshot())
        if after['tables']!=before['tables']:raise AssertionError('Pre-existing business facts changed')
        async def unknown_legacy_facts():
            db=configured_database()
            try:
                async with db.sessions() as session:
                    unexpected=await session.scalar(select(func.count()).select_from(Order).where(or_(
                        Order.shipped_at.is_not(None),Order.delivered_at.is_not(None),Order.refunded_amount.is_not(None),
                        Order.refund_reserved_amount.is_not(None),Order.return_policy!='unknown',Order.return_condition!='unknown',Order.fact_version!=1)))
                return unexpected==0
            finally:await db.close()
        report['legacy_order_facts_unknown']=asyncio.run(unknown_legacy_facts())
        if not report['legacy_order_facts_unknown']:raise AssertionError('Unexpected historical order fact backfill')
        async def seed():
            db=configured_database()
            try:
                first=await seed_order_scenarios(db);second=await seed_order_scenarios(db)
                if first['inserted']!=14 or second['inserted']!=0 or second['retained']!=14:raise AssertionError('Unexpected order seed result')
                return first,second,await db.summary()
            finally:await db.close()
        report['stage']='seed'
        first,second,summary=asyncio.run(seed());report.update(first_seed=first,second_seed=second,database=summary)
        added=[row.facts.order_id for row in load_order_scenarios()[1]]
        preserved=asyncio.run(snapshot(added))
        if preserved['tables']!=before['tables']:raise AssertionError('Existing rows changed after inserting scenarios')
        report.update(passed=True,stage='complete',revision=after['revision'],preexisting_rows_preserved=True,seed_idempotent=True)
    except Exception as error:report['error_type']=type(error).__name__
    (output/'live-upgrade.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='before'},indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
