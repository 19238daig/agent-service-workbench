"""Never put database credentials in alembic.ini or log the configured URL."""
import os
from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool
from smart_cs.db.models import Base
from smart_cs.settings import ENV_PATH

load_dotenv(ENV_PATH)
url = os.environ.get('DATABASE_URL', '')
if not url:
    raise RuntimeError('Set DATABASE_URL in the root .env before migrating')
url = url.replace('postgresql+asyncpg://', 'postgresql+psycopg://', 1).replace('sqlite+aiosqlite://', 'sqlite://', 1)

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = context.config.attributes.get('connection')
    def run(connection):
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True,
                          version_table_schema=context.config.attributes.get('version_table_schema'))
        with context.begin_transaction():
            context.run_migrations()
    if supplied is not None:
        run(supplied)
    else:
        engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
        with engine.connect() as connection:
            run(connection)
