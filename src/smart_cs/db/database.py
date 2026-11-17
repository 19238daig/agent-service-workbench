from __future__ import annotations

import os
from sqlalchemy import event, func, select, text, inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_cs.db.models import Base, DatasetRelease, DatasetSample, KnowledgeChunk, KnowledgeDocument, Order, Ticket, TicketEvent, User, ChatSession, ChatMessage, ExecutionRun, ModelCallRecord, RetrievalIndex, KnowledgeEmbedding
from smart_cs.db.models import AuthCredential, AuthSession, AuthLoginAttempt, ChatTurn, KnowledgeIngestion, RetrievalIndexDocument
from smart_cs.db.models import ActionRequest,Approval,ActionExecution,BackgroundJob,BackgroundJobEvent,RunControl,RunEvent,KnowledgeAdminEvent


class Database:
    def __init__(self, url: str, schema: str | None = None):
        self.schema=schema
        if not url.startswith(('postgresql+asyncpg://', 'sqlite+aiosqlite://')):
            raise ValueError('Use postgresql+asyncpg or the explicit SQLite test adapter')
        options = {}
        if schema:
            import re
            if not re.fullmatch(r'foundation_verify_[a-f0-9]{12}', schema) or not url.startswith('postgresql+'):
                raise ValueError('Only an isolated verification schema may be selected')
            options['connect_args'] = {'server_settings': {'search_path': schema + ',public'}}
            # search_path alone is NOT isolation: SQLAlchemy's checkfirst/drop_all
            # may resolve missing tables from public. Qualify every ORM/Core table.
            options['execution_options'] = {'schema_translate_map': {None: schema}}
        self.engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True, **options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if self.engine.dialect.name == 'sqlite':
            @event.listens_for(self.engine.sync_engine, 'connect')
            def configure_sqlite(connection, _record):
                connection.execute('PRAGMA foreign_keys=ON')

    async def initialize(self, create_schema=False):
        async with self.engine.begin() as connection:
            if create_schema:
                await connection.run_sync(Base.metadata.create_all)
            await connection.execute(select(User.id).limit(1))

    async def close(self):
        await self.engine.dispose()

    async def summary(self):
        tables = [User, Order, Ticket, TicketEvent, ChatSession, ChatMessage, ChatTurn, KnowledgeDocument, KnowledgeChunk, KnowledgeIngestion, DatasetRelease, DatasetSample,RetrievalIndex,RetrievalIndexDocument,KnowledgeEmbedding,ExecutionRun,ModelCallRecord,AuthCredential,AuthSession,AuthLoginAttempt,ActionRequest,Approval,ActionExecution,BackgroundJob,BackgroundJobEvent,RunControl,RunEvent,KnowledgeAdminEvent]
        async with self.engine.connect() as connection:
            available=await connection.run_sync(lambda conn:set(inspect(conn).get_table_names(schema=self.schema)))
        async with self.sessions() as session:
            counts = {table.__tablename__: await session.scalar(select(func.count()).select_from(table)) for table in tables if table.__tablename__ in available}
            extension = None
            if self.engine.dialect.name == 'postgresql':
                extension = await session.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        return {'backend': self.engine.dialect.name, 'pgvector_version': extension, 'counts': counts,
                'missing_tables':[table.__tablename__ for table in tables if table.__tablename__ not in available]}

    async def knowledge(self):
        async with self.sessions() as session:
            # The lexical/offline fallback never loads restricted content into a shared cache.
            documents = (await session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.status == 'published', KnowledgeDocument.access_scope == 'public').order_by(KnowledgeDocument.id))).all()
            return [{'id': d.id, 'content': d.content, 'source': d.source,
                     'metadata': {'version': d.version, 'effective_from': d.effective_from, 'source_type': d.source_type}} for d in documents]


def configured_database():
    url = os.getenv('DATABASE_URL', '').strip()
    return Database(url) if url else None
