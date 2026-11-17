"""Real PostgreSQL integration checks in a fresh, isolated verification schema."""
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from smart_cs.db.database import Database
from smart_cs.db.seed import seed_database
from smart_cs.db.tickets import DatabaseTicketStore
from smart_cs.settings import ENV_PATH, PROJECT_ROOT

load_dotenv(ENV_PATH)
url = os.environ['DATABASE_URL']
if not url.startswith('postgresql+asyncpg://'):
    raise RuntimeError('This integration check requires the configured PostgreSQL service')
schema = 'foundation_verify_' + uuid.uuid4().hex[:12]
assert re.fullmatch(r'foundation_verify_[a-f0-9]{12}', schema)
engine = create_engine(url.replace('+asyncpg', '+psycopg', 1), hide_parameters=True)
report = {'passed': False, 'backend': 'postgresql', 'schema_isolation': True}


async def verify():
    db = Database(url, schema=schema)
    try:
        await db.initialize()
        first = await seed_database(db)
        ticket = await DatabaseTicketStore(db).create('refund', 'medium', '跨连接持久化验证', '合成验证申请', 'verification-owner')
        await db.close()
        db = Database(url, schema=schema)
        await db.initialize()
        store = DatabaseTicketStore(db)
        assert (await store.query(ticket['ticket_id'], 'verification-owner'))['summary'] == '跨连接持久化验证'
        assert await store.query(ticket['ticket_id'], 'another-owner') is None
        await store.update_status(ticket['ticket_id'], 'processing', 'verification-owner')
        second = await seed_database(db)
        assert second['database']['counts']['tickets'] == 501
        assert second['database']['counts']['ticket_events'] == 502
        assert second['database']['counts']['dataset_samples'] == 670
        report.update(passed=True, initial=first['database'], final=second['database'],
                      reconnect_persistence=True, owner_filter=True, seed_idempotent=True, atomic_ticket_events=True)
    finally:
        await db.close()


created = False
try:
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        created = True
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", public')
        config = Config(str(PROJECT_ROOT / 'alembic.ini'))
        config.attributes['connection'] = connection
        config.attributes['version_table_schema'] = schema
        command.upgrade(config, 'head')
    asyncio.run(verify())
except Exception as error:
    report['error_type'] = type(error).__name__
finally:
    if created:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
    engine.dispose()
    path = PROJECT_ROOT / '.local/database-foundation-verification.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report['passed'] else 1)
