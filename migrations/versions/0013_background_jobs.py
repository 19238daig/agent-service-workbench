"""Durable job outbox, finite attempts and ordered progress events."""
from alembic import op
import sqlalchemy as sa

revision='0013_background_jobs'
down_revision='0012_action_execution'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('background_jobs',
        sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('user_id',sa.String(128),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('idempotency_key',sa.String(100),nullable=False),
        sa.Column('kind',sa.String(32),nullable=False),
        sa.Column('payload',sa.JSON(),nullable=False),sa.Column('payload_hash',sa.String(64),nullable=False),
        sa.Column('binding',sa.JSON(),nullable=False),sa.Column('status',sa.String(24),nullable=False),
        sa.Column('attempt',sa.Integer(),nullable=False),sa.Column('max_attempts',sa.Integer(),nullable=False),
        sa.Column('attempt_token',sa.String(32)),sa.Column('worker',sa.String(160)),
        sa.Column('heartbeat_at',sa.DateTime(timezone=True)),sa.Column('lease_until',sa.DateTime(timezone=True)),
        sa.Column('next_dispatch_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('dispatch_count',sa.Integer(),nullable=False),sa.Column('stage',sa.String(40),nullable=False),
        sa.Column('progress',sa.Integer(),nullable=False),sa.Column('total',sa.Integer(),nullable=False),
        sa.Column('checkpoint',sa.JSON(),nullable=False),sa.Column('result',sa.JSON()),sa.Column('error_code',sa.String(64)),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('user_id','idempotency_key',name='uq_job_idempotency'),
        sa.CheckConstraint("kind IN ('ingest','index','retrieval_evaluation')",name='ck_job_kind'),
        sa.CheckConstraint("status IN ('queued','running','retry_wait','succeeded','failed','cancel_requested','cancelled')",name='ck_job_status'),
        sa.CheckConstraint('attempt >= 0 AND attempt <= max_attempts AND max_attempts BETWEEN 1 AND 3',name='ck_job_attempts'),
        sa.CheckConstraint('progress >= 0 AND total >= progress',name='ck_job_progress'),
        sa.CheckConstraint('version >= 1 AND dispatch_count >= 0',name='ck_job_version'))
    op.create_index('ix_job_dispatch','background_jobs',['status','next_dispatch_at'])
    op.create_index('ix_job_owner_created','background_jobs',['user_id','created_at'])
    op.create_table('background_job_events',
        sa.Column('id',sa.String(32),primary_key=True),sa.Column('job_id',sa.String(32),sa.ForeignKey('background_jobs.id'),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),sa.Column('event_type',sa.String(40),nullable=False),
        sa.Column('payload',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('job_id','version',name='uq_job_event_version'),sa.CheckConstraint('version >= 1',name='ck_job_event_version'))
    # This narrowly scoped function locks authority rows without exposing password
    # hashes or granting worker UPDATE privileges on users/auth tables.
    op.execute('''CREATE FUNCTION public.smart_cs_worker_admin(actor text) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,public AS $$
        DECLARE found_id text;
        BEGIN
            SELECT u.id INTO found_id FROM public.users u JOIN public.auth_credentials c ON c.user_id=u.id
            WHERE u.id=actor AND u.role='admin' AND c.enabled FOR SHARE OF u,c;
            RETURN found_id IS NOT NULL;
        END $$''')
    op.execute('REVOKE ALL ON FUNCTION public.smart_cs_worker_admin(text) FROM PUBLIC')

def downgrade():raise RuntimeError('Archive background job evidence before an explicit downgrade')
