"""Idempotent inserts: re-running fixtures never resets an existing order/ticket."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select

from smart_cs.datasets import DATA_ROOT, VERSION, load_samples, sha, validate_assets
from smart_cs.db.models import DatasetRelease, DatasetSample, KnowledgeChunk, KnowledgeDocument, Order, Ticket, TicketEvent, User,ChatSession,ChatMessage


async def seed_database(database, root=DATA_ROOT, *, include_conversations=True):
    validation = validate_assets(root)
    manifest, samples = load_samples(root)
    manifest_hash = sha((root / 'manifest.json').read_bytes())
    async with database.sessions.begin() as session:
        release = await session.get(DatasetRelease, VERSION)
        if release and release.manifest_hash != manifest_hash:
            raise ValueError('Immutable dataset release changed; publish a new version')
        if not release:
            session.add(DatasetRelease(id=VERSION, version='v1', manifest_hash=manifest_hash,
                        provenance='ai_authored_synthetic_not_human_reviewed', manifest=manifest))
            await session.flush()
            session.add_all([DatasetSample(id=s['id'], release_id=VERSION, task=s['task'], split=s['split'],
                group_id=s['group_id'], label=s['label'], payload=s) for s in samples])

        users = set((await session.scalars(select(User.id))).all())
        for i in range(1, 21):
            identifier = f'demo-user-{i:03d}'
            if identifier not in users:
                session.add(User(id=identifier, display_name=f'合成演示用户{i:03d}', role='customer', source='synthetic_fixture'))
        await session.flush()
        orders = set((await session.scalars(select(Order.id))).all())
        fixed = datetime(2026, 11, 13, tzinfo=timezone.utc)
        for i in range(1, 201):
            identifier = f'DEMO-{i:06d}'
            if identifier not in orders:
                session.add(Order(id=identifier, user_id=f'demo-user-{(i-1)%20+1:03d}',
                    product_name='虚构演示耳机' if i % 2 else '虚构演示配件', paid_amount=Decimal('99.00') + Decimal(i),
                    currency='CNY', status=['pending_shipment', 'shipped', 'delivered', 'cancelled'][i % 4],
                    paid_at=fixed - timedelta(days=i % 30), source='synthetic_fixture'))
        await session.flush()
        tickets = set((await session.scalars(select(Ticket.id))).all())
        for i in range(1, 501):
            identifier = f'TK-20261113-{i:06X}'
            if identifier not in tickets:
                order_index = (i - 1) % 200 + 1
                user_id = f'demo-user-{(order_index-1)%20+1:03d}'
                status = ['created', 'processing', 'pending_review', 'resolved', 'closed'][i % 5]
                session.add(Ticket(id=identifier, user_id=user_id, order_id=f'DEMO-{order_index:06d}',
                    type='refund' if i % 2 else 'complaint', priority='medium', status=status,
                    summary=f'合成演示工单{i:03d}', details='用于本地开发和集成验证，不对应真实客户或真实支付。',
                    source='synthetic_fixture', created_at=fixed - timedelta(minutes=i), updated_at=fixed, version=1))
                await session.flush()
                session.add(TicketEvent(id=f'fixture-created-{i:06d}', ticket_id=identifier, actor_id=user_id,
                    event_type='fixture_snapshot', from_status=None, to_status=status, created_at=fixed))

        documents = json.loads((root / 'knowledge/documents.json').read_text(encoding='utf-8'))
        existing = set((await session.scalars(select(KnowledgeDocument.id))).all())
        for doc in documents:
            if doc['id'] not in existing:
                session.add(KnowledgeDocument(**doc))
                await session.flush()
                # Small starter policies deliberately remain whole semantic units.
                session.add(KnowledgeChunk(id=doc['id'] + '-000', document_id=doc['id'], ordinal=0,
                    content=doc['content'], content_hash=doc['content_hash'], embedding_status='pending'))
        if include_conversations:
            existing_sessions=set((await session.scalars(select(ChatSession.id))).all())
            for i in range(1,21):
                identifier=f'fixture-session-{i:03d}';user_id=f'demo-user-{i:03d}'
                # Never reset counters or repopulate an existing/edited conversation.
                if identifier in existing_sessions:continue
                session.add(ChatSession(id=identifier,user_id=user_id,title=f'合成历史样例{i:03d}',status='active',
                    graph_version='four-agents-v1',next_sequence=3,version=1,source='synthetic_fixture',created_at=fixed,updated_at=fixed))
                await session.flush()
                session.add_all([
                    ChatMessage(id=f'fixture-message-{i:03d}-1',session_id=identifier,user_id=user_id,sequence=1,
                        role='user',content='这是用于数据库验证的合成对话样例。',source='synthetic_fixture',created_at=fixed),
                    ChatMessage(id=f'fixture-message-{i:03d}-2',session_id=identifier,user_id=user_id,sequence=2,
                        role='assistant',content='这是合成历史回复，不代表真实客服办理结果。',source='synthetic_fixture',created_at=fixed),
                ])
    return {'dataset_validation': validation, 'database': await database.summary()}
