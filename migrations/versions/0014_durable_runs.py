"""Durable conversational commands and ordered public events, preserving old runs."""
from alembic import op
import sqlalchemy as sa

revision='0014_durable_runs'
down_revision='0013_background_jobs'
branch_labels=None
depends_on=None

def upgrade():
    op.drop_constraint('ck_run_status','execution_runs',type_='check')
    op.create_check_constraint('ck_run_status','execution_runs',"status IN ('queued','running','succeeded','blocked','failed','cancelled','waiting_approval','interrupted')")
    op.drop_constraint('ck_run_completion','execution_runs',type_='check')
    op.create_check_constraint('ck_run_completion','execution_runs',"(status IN ('queued','running') AND finished_at IS NULL) OR (status NOT IN ('queued','running') AND finished_at IS NOT NULL)")
    op.create_table('run_controls',
        sa.Column('run_id',sa.String(32),primary_key=True),sa.Column('user_id',sa.String(128),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),sa.Column('turn_id',sa.String(32),sa.ForeignKey('chat_turns.id'),nullable=False),
        sa.Column('auth_session_id',sa.String(32),sa.ForeignKey('auth_sessions.id'),nullable=True),
        sa.Column('client_request_id',sa.String(100),nullable=False),sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('kind',sa.String(8),nullable=False),sa.Column('status',sa.String(24),nullable=False),
        sa.Column('attempt_token',sa.String(32)),sa.Column('heartbeat_at',sa.DateTime(timezone=True)),sa.Column('lease_until',sa.DateTime(timezone=True)),
        sa.Column('cancel_code',sa.String(64)),sa.Column('next_event_sequence',sa.Integer(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['run_id','session_id','user_id'],['execution_runs.id','execution_runs.session_id','execution_runs.subject_id'],name='fk_control_run_owner'),
        sa.UniqueConstraint('user_id','client_request_id',name='uq_control_client_request'),
        sa.CheckConstraint("kind IN ('new','resume')",name='ck_control_kind'),
        sa.CheckConstraint("status IN ('queued','running','cancel_requested','succeeded','blocked','failed','cancelled','waiting_approval','interrupted')",name='ck_control_status'),
        sa.CheckConstraint('next_event_sequence >= 1',name='ck_control_sequence'))
    op.create_index('ix_control_dispatch','run_controls',['status','lease_until'])
    op.create_index('ix_control_owner_created','run_controls',['user_id','created_at'])
    op.create_table('run_events',sa.Column('run_id',sa.String(32),sa.ForeignKey('run_controls.run_id'),primary_key=True),
        sa.Column('sequence',sa.Integer(),primary_key=True),sa.Column('event_type',sa.String(32),nullable=False),
        sa.Column('payload',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('sequence >= 1',name='ck_run_event_sequence'))

def downgrade():raise RuntimeError('Archive public run events before an explicit downgrade')
