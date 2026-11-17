"""Versioned business, knowledge and labelled dataset foundation.

Revision ID: 0001_foundation
Revises: None
"""
from alembic import op
import sqlalchemy as sa

revision = '0001_foundation'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # This revision is deliberately explicit; future ORM changes cannot alter it.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table('users', sa.Column('id', sa.String(128), primary_key=True), sa.Column('display_name', sa.String(100), nullable=False), sa.Column('role', sa.String(20), nullable=False), sa.Column('source', sa.String(32), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("role IN ('customer','support','admin')", name='ck_user_role'))
    op.create_table('orders', sa.Column('id', sa.String(64), primary_key=True), sa.Column('user_id', sa.String(128), sa.ForeignKey('users.id'), nullable=False), sa.Column('product_name', sa.String(120), nullable=False), sa.Column('paid_amount', sa.Numeric(12,2), nullable=False), sa.Column('currency', sa.String(3), nullable=False), sa.Column('status', sa.String(32), nullable=False), sa.Column('paid_at', sa.DateTime(timezone=True), nullable=False), sa.Column('source', sa.String(32), nullable=False), sa.CheckConstraint('paid_amount >= 0', name='ck_order_amount'))
    op.create_index('ix_orders_user_id', 'orders', ['user_id'])
    op.create_table('tickets', sa.Column('id', sa.String(40), primary_key=True), sa.Column('user_id', sa.String(128), sa.ForeignKey('users.id'), nullable=False), sa.Column('order_id', sa.String(64), sa.ForeignKey('orders.id')), sa.Column('type', sa.String(32), nullable=False), sa.Column('priority', sa.String(16), nullable=False), sa.Column('status', sa.String(32), nullable=False), sa.Column('summary', sa.String(500), nullable=False), sa.Column('details', sa.Text(), nullable=False), sa.Column('source', sa.String(32), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.Column('version', sa.Integer(), nullable=False), sa.CheckConstraint("priority IN ('low','medium','high','urgent')", name='ck_ticket_priority'), sa.CheckConstraint("status IN ('created','processing','pending_review','resolved','closed','escalated')", name='ck_ticket_status'))
    op.create_index('ix_ticket_owner_created', 'tickets', ['user_id','created_at'])
    op.create_table('ticket_events', sa.Column('id', sa.String(64), primary_key=True), sa.Column('ticket_id', sa.String(40), sa.ForeignKey('tickets.id'), nullable=False), sa.Column('actor_id', sa.String(128), sa.ForeignKey('users.id'), nullable=False), sa.Column('event_type', sa.String(32), nullable=False), sa.Column('from_status', sa.String(32)), sa.Column('to_status', sa.String(32), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_ticket_events_ticket_id','ticket_events',['ticket_id'])
    op.create_table('knowledge_documents', sa.Column('id',sa.String(80),primary_key=True), sa.Column('source',sa.String(180),nullable=False),sa.Column('title',sa.String(180),nullable=False),sa.Column('version',sa.String(32),nullable=False),sa.Column('domain',sa.String(60),nullable=False),sa.Column('status',sa.String(20),nullable=False),sa.Column('content',sa.Text(),nullable=False),sa.Column('content_hash',sa.String(64),nullable=False),sa.Column('effective_from',sa.String(10),nullable=False),sa.Column('source_type',sa.String(32),nullable=False))
    op.create_table('knowledge_chunks',sa.Column('id',sa.String(100),primary_key=True),sa.Column('document_id',sa.String(80),sa.ForeignKey('knowledge_documents.id'),nullable=False),sa.Column('ordinal',sa.Integer(),nullable=False),sa.Column('content',sa.Text(),nullable=False),sa.Column('content_hash',sa.String(64),nullable=False),sa.Column('embedding_status',sa.String(24),nullable=False),sa.UniqueConstraint('document_id','ordinal',name='uq_chunk_position'))
    op.create_index('ix_knowledge_chunks_document_id','knowledge_chunks',['document_id'])
    op.create_table('dataset_releases',sa.Column('id',sa.String(80),primary_key=True),sa.Column('version',sa.String(32),nullable=False),sa.Column('manifest_hash',sa.String(64),nullable=False),sa.Column('provenance',sa.String(100),nullable=False),sa.Column('manifest',sa.JSON(),nullable=False))
    op.create_table('dataset_samples',sa.Column('id',sa.String(128),primary_key=True),sa.Column('release_id',sa.String(80),sa.ForeignKey('dataset_releases.id'),nullable=False),sa.Column('task',sa.String(32),nullable=False),sa.Column('split',sa.String(16),nullable=False),sa.Column('group_id',sa.String(128),nullable=False),sa.Column('label',sa.String(64),nullable=False),sa.Column('payload',sa.JSON(),nullable=False),sa.CheckConstraint("split IN ('train','validation','test','challenge')",name='ck_dataset_split'))
    op.create_index('ix_dataset_task_split','dataset_samples',['task','split'])


def downgrade():
    for name in ['dataset_samples','dataset_releases','knowledge_chunks','knowledge_documents','ticket_events','tickets','orders','users']:
        op.drop_table(name)
    # The shared pgvector extension is intentionally retained.
