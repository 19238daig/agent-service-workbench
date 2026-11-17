"""Setup, inspect and validate the data foundation without calling a paid LLM."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
from dotenv import dotenv_values, load_dotenv, set_key

from smart_cs.datasets import DATA_ROOT, build_assets, validate_assets
from smart_cs.db.database import configured_database
from smart_cs.db.seed import seed_database
from smart_cs.settings import ENV_PATH, PROJECT_ROOT


def configure_local():
    """Only add missing database keys; existing model credentials are untouched."""
    values = dotenv_values(ENV_PATH)
    password = values.get('POSTGRES_PASSWORD') or secrets.token_hex(24)
    defaults = {'POSTGRES_USER': 'smart_cs', 'POSTGRES_DB': 'smart_cs',
                'POSTGRES_PASSWORD': password, 'POSTGRES_PORT': '55432', 'REDIS_PORT': '56379',
                'DATABASE_URL': f'postgresql+asyncpg://smart_cs:{password}@127.0.0.1:55432/smart_cs',
                'FOUNDATION_AUTO_START': 'true', 'DATABASE_AUTO_CREATE': 'false', 'APP_MODE': 'local_demo'}
    for key, value in defaults.items():
        if not values.get(key):
            set_key(str(ENV_PATH), key, value, quote_mode='always')
    if values.get('REDIS_URL') in {None, '', 'redis://localhost:6379/0', 'redis://127.0.0.1:6379/0'}:
        set_key(str(ENV_PATH), 'REDIS_URL', 'redis://127.0.0.1:56379/0', quote_mode='always')
    print('Database configuration saved in the root .env (credentials hidden).')


def ensure_services():
    command = ['docker', 'compose', '-p', 'smart-cs-foundation', '-f', str(PROJECT_ROOT / 'docker-compose.yml'), 'up', '-d', '--wait', '--wait-timeout', '45', 'postgres', 'redis']
    result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Docker data services unavailable; start Docker Desktop and run the setup command again')


def migrate():
    from alembic import command
    from alembic.config import Config
    command.upgrade(Config(str(PROJECT_ROOT / 'alembic.ini')), 'head')


async def prepare_database():
    database = configured_database()
    if database is None:
        raise ValueError('DATABASE_URL is not configured')
    try:
        await database.initialize()
        result=await seed_database(database)
        from smart_cs.order_fixtures import seed_order_scenarios
        result['order_scenarios']=await seed_order_scenarios(database)
        from smart_cs.ticket_fixtures import seed_ticket_scenarios
        result['ticket_scenarios']=await seed_ticket_scenarios(database)
        from smart_cs.persistence.postgres import prepare_checkpoint_store
        result['checkpoints']=await prepare_checkpoint_store(database)
        result['database']=await database.summary()
        return result
    finally:
        await database.close()


async def inspect_database():
    database = configured_database()
    if database is None:
        return {'enabled': False}
    try:
        await database.initialize()
        return await database.summary()
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'build-data', 'validate-data', 'prepare', 'status'])
    parser.add_argument('--start-services', action='store_true')
    args = parser.parse_args()
    load_dotenv(ENV_PATH)
    try:
        if args.action == 'configure':
            configure_local()
            return 0
        if args.action == 'build-data':
            result = build_assets()
        elif args.action == 'validate-data':
            result = validate_assets()
        elif args.action == 'status':
            result = asyncio.run(inspect_database())
        else:
            build_assets()
            if args.start_services:
                ensure_services()
            # A bounded database readiness check, never print connection URLs.
            import time
            from sqlalchemy import create_engine, text
            url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql+psycopg://', 1).replace('sqlite+aiosqlite://', 'sqlite://', 1)
            for attempt in range(20):
                engine = create_engine(url, hide_parameters=True)
                try:
                    with engine.connect() as connection:
                        connection.execute(text('SELECT 1'))
                    break
                except Exception:
                    if attempt == 19:
                        raise RuntimeError('Database did not become ready') from None
                    time.sleep(1)
                finally:
                    engine.dispose()
            migrate()
            result = asyncio.run(prepare_database())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == 'prepare':
            report = PROJECT_ROOT / '.local' / 'foundation-setup.json'
            report.parent.mkdir(exist_ok=True)
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0
    except Exception as error:
        # Driver errors may embed URLs/credentials. Do not echo their message.
        print(f'Foundation setup failed ({type(error).__name__}); check data services, migrations and the root configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
