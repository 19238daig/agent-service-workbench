"""Durable reviewed drafts, human decisions and paused turns."""
from alembic import op
import sqlalchemy as sa

revision='0011_action_approvals'
down_revision='0010_dense_retrieval'
branch_labels=None
depends_on=None


def upgrade():
    with op.batch_alter_table('chat_turns') as batch:
        batch.drop_constraint('ck_turn_status',type_='check')
        batch.drop_constraint('ck_turn_publication',type_='check')
        batch.create_check_constraint('ck_turn_status',"status IN ('running','completed','blocked','failed','cancelled','interrupted','waiting_approval')")
        batch.create_check_constraint('ck_turn_publication',"(status IN ('completed','blocked','waiting_approval') AND assistant_message_id IS NOT NULL) OR (status NOT IN ('completed','blocked','waiting_approval') AND assistant_message_id IS NULL)")
    with op.batch_alter_table('execution_runs') as batch:
        batch.drop_constraint('ck_run_status',type_='check')
        batch.create_check_constraint('ck_run_status',"status IN ('running','succeeded','blocked','failed','cancelled','waiting_approval')")
    op.create_table('action_requests',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('session_id',sa.String(128),nullable=False),sa.Column('user_id',sa.String(128),nullable=False),
        sa.Column('turn_id',sa.String(32),sa.ForeignKey('chat_turns.id'),nullable=False,unique=True),
        sa.Column('actor_role',sa.String(20),nullable=False),sa.Column('tool',sa.String(40),nullable=False),
        sa.Column('parameter_hash',sa.String(64),nullable=False),sa.Column('draft_hash',sa.String(64),nullable=False),
        sa.Column('snapshot',sa.JSON(),nullable=False),sa.Column('required_approval',sa.String(8),nullable=False),
        sa.Column('status',sa.String(24),nullable=False),sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['session_id','user_id'],['chat_sessions.id','chat_sessions.user_id'],name='fk_action_session_owner'),
        sa.CheckConstraint("actor_role IN ('customer','support','admin')",name='ck_action_role'),
        sa.CheckConstraint("tool IN ('ticket_create','ticket_transition')",name='ck_action_tool'),
        sa.CheckConstraint("required_approval IN ('user','staff')",name='ck_action_requirement'),
        sa.CheckConstraint("status IN ('awaiting_user','awaiting_staff','approved','ready','rejected','cancelled','expired','stale')",name='ck_action_status'),
        sa.CheckConstraint('expires_at > created_at',name='ck_action_expiry'))
    op.create_index('ix_action_owner_created','action_requests',['user_id','created_at'])
    op.create_index('ix_action_queue','action_requests',['status','expires_at'])
    op.create_table('approvals',
        sa.Column('id',sa.String(32),primary_key=True),sa.Column('action_id',sa.String(64),sa.ForeignKey('action_requests.id'),nullable=False),
        sa.Column('stage',sa.String(8),nullable=False),sa.Column('principal_id',sa.String(128),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('principal_role',sa.String(20),nullable=False),sa.Column('decision',sa.String(8),nullable=False),
        sa.Column('parameter_hash',sa.String(64),nullable=False),sa.Column('draft_hash',sa.String(64),nullable=False),
        sa.Column('note',sa.String(500),nullable=False),sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('action_id','stage',name='uq_approval_stage'),
        sa.CheckConstraint("stage IN ('user','staff')",name='ck_approval_stage'),
        sa.CheckConstraint("decision IN ('approve','reject')",name='ck_approval_decision'),
        sa.CheckConstraint("principal_role IN ('customer','support','admin')",name='ck_approval_role'),
        sa.CheckConstraint("stage <> 'staff' OR principal_role IN ('support','admin')",name='ck_approval_staff'),
        sa.CheckConstraint('expires_at > created_at',name='ck_approval_expiry'))


def downgrade():
    # Downgrading active approval workflows would discard consent evidence.
    raise RuntimeError('Approval migration requires an explicit archival migration to downgrade')
