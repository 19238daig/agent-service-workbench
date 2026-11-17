"""Core session/message tables and integrity constraints; no historical data rewrite."""
from alembic import op
import sqlalchemy as sa

revision='0004_core_sessions'
down_revision='0003_run_observability'
branch_labels=None
depends_on=None

STATES="('created','processing','pending_review','resolved','closed','escalated')"
CHECKS={
 'orders':[
    ('ck_order_amount_max','paid_amount <= 9999999999.99'),
    ('ck_order_currency',"currency = 'CNY'"),
    ('ck_order_status',"status IN ('pending_shipment','shipped','delivered','cancelled')")],
 'tickets':[
    ('ck_ticket_type',"type IN ('refund','claim','account_open','account_change','complaint','general')"),
    ('ck_ticket_version','version >= 1')],
 'ticket_events':[
    ('ck_event_from_status','from_status IS NULL OR from_status IN '+STATES),
    ('ck_event_to_status','to_status IN '+STATES)],
 'knowledge_chunks':[
    ('ck_chunk_ordinal','ordinal >= 0'),('ck_chunk_page','source_page IS NULL OR source_page >= 1'),
    ('ck_chunk_tokens','token_count IS NULL OR token_count >= 0')],
 'execution_runs':[
    ('ck_run_status',"status IN ('running','succeeded','blocked','failed','cancelled')"),
    ('ck_run_completion',"(status = 'running' AND finished_at IS NULL) OR (status <> 'running' AND finished_at IS NOT NULL)")],
 'model_call_records':[
    ('ck_call_kind',"kind IN ('chat','embedding','reranker')"),
    ('ck_call_status',"status IN ('succeeded','failed','cancelled')"),
    ('ck_call_duration','duration_ms >= 0 AND duration_ms <= 99999999999.999')],
}


def preflight():
    connection=op.get_bind()
    for table,checks in CHECKS.items():
        for name,expression in checks:
            if connection.execute(sa.text(f'SELECT count(*) FROM {table} WHERE NOT ({expression})')).scalar():
                raise RuntimeError('Existing rows violate '+name+'; migration stopped without rewriting business data')
    guards=[
        "SELECT count(*) FROM tickets t JOIN orders o ON o.id=t.order_id WHERE t.user_id<>o.user_id",
        "SELECT count(*) FROM model_call_records c JOIN execution_runs r ON c.run_id=r.id WHERE c.request_id<>r.request_id OR c.trace_id<>r.trace_id",
        "SELECT count(*) FROM (SELECT document_id,ordinal FROM knowledge_chunks WHERE chunk_set IS NULL GROUP BY document_id,ordinal HAVING count(*)>1) duplicates",
    ]
    if any(connection.execute(sa.text(query)).scalar() for query in guards):
        raise RuntimeError('Existing relation/uniqueness violations; migration stopped without rewriting business data')


def upgrade():
    preflight()
    for table,checks in CHECKS.items():
        with op.batch_alter_table(table) as batch:
            for name,expression in checks:batch.create_check_constraint(name,expression)
    with op.batch_alter_table('orders') as batch:batch.create_unique_constraint('uq_order_owner',['id','user_id'])
    op.create_index('ix_order_owner_paid','orders',['user_id','paid_at'])
    with op.batch_alter_table('tickets') as batch:
        batch.create_foreign_key('fk_ticket_order_owner','orders',['order_id','user_id'],['id','user_id'])
    op.create_index('uq_legacy_chunk_position','knowledge_chunks',['document_id','ordinal'],unique=True,postgresql_where=sa.text('chunk_set IS NULL'),sqlite_where=sa.text('chunk_set IS NULL'))
    with op.batch_alter_table('execution_runs') as batch:batch.create_unique_constraint('uq_run_correlation',['id','request_id','trace_id'])
    with op.batch_alter_table('model_call_records') as batch:
        batch.create_foreign_key('fk_call_run_correlation','execution_runs',['run_id','request_id','trace_id'],['id','request_id','trace_id'],ondelete='CASCADE')
    op.create_table('chat_sessions',sa.Column('id',sa.String(128),primary_key=True),
        sa.Column('user_id',sa.String(128),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('title',sa.String(200),nullable=False),sa.Column('status',sa.String(16),nullable=False),
        sa.Column('graph_version',sa.String(64),nullable=False),sa.Column('next_sequence',sa.Integer(),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),sa.Column('source',sa.String(32),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('id','user_id',name='uq_session_owner'),sa.CheckConstraint("status IN ('active','archived')",name='ck_session_status'),
        sa.CheckConstraint('next_sequence >= 1',name='ck_session_next_sequence'),sa.CheckConstraint('version >= 1',name='ck_session_version'))
    op.create_index('ix_session_owner_updated','chat_sessions',['user_id','updated_at'])
    op.create_table('chat_messages',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('session_id',sa.String(128),nullable=False),sa.Column('user_id',sa.String(128),nullable=False),
        sa.Column('sequence',sa.Integer(),nullable=False),sa.Column('role',sa.String(16),nullable=False),
        sa.Column('content',sa.Text(),nullable=False),sa.Column('source',sa.String(32),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['session_id','user_id'],['chat_sessions.id','chat_sessions.user_id'],name='fk_message_session_owner',ondelete='CASCADE'),
        sa.UniqueConstraint('session_id','sequence',name='uq_message_session_sequence'),
        sa.CheckConstraint('sequence >= 1',name='ck_message_sequence'),sa.CheckConstraint("role IN ('user','assistant')",name='ck_message_role'),
        sa.CheckConstraint('length(trim(content)) > 0',name='ck_message_content'))
    op.create_index('ix_message_owner_created','chat_messages',['user_id','created_at'])


def downgrade():
    connection=op.get_bind()
    if connection.execute(sa.text('SELECT count(*) FROM chat_sessions')).scalar():
        raise RuntimeError('Session data exists; restore to an isolated database instead of dropping persisted messages')
    op.drop_table('chat_messages');op.drop_table('chat_sessions')
    with op.batch_alter_table('model_call_records') as batch:batch.drop_constraint('fk_call_run_correlation',type_='foreignkey')
    with op.batch_alter_table('execution_runs') as batch:batch.drop_constraint('uq_run_correlation',type_='unique')
    op.drop_index('uq_legacy_chunk_position',table_name='knowledge_chunks')
    with op.batch_alter_table('tickets') as batch:batch.drop_constraint('fk_ticket_order_owner',type_='foreignkey')
    op.drop_index('ix_order_owner_paid',table_name='orders')
    with op.batch_alter_table('orders') as batch:batch.drop_constraint('uq_order_owner',type_='unique')
    for table,checks in reversed(list(CHECKS.items())):
        with op.batch_alter_table(table) as batch:
            for name,_ in checks:batch.drop_constraint(name,type_='check')
