"""Atomic action receipts and explicitly tracked refund reservations."""
from alembic import op
import sqlalchemy as sa

revision='0012_action_execution'
down_revision='0011_action_approvals'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('tickets',sa.Column('requested_amount',sa.Numeric(12,2),nullable=True))
    op.create_check_constraint('ck_ticket_requested_amount','tickets',"requested_amount IS NULL OR (type = 'refund' AND requested_amount > 0 AND requested_amount <= 9999999999.99)")
    op.add_column('ticket_events',sa.Column('action_id',sa.String(64),nullable=True))
    op.create_foreign_key('fk_event_action','ticket_events','action_requests',['action_id'],['id'])
    op.create_unique_constraint('uq_event_action','ticket_events',['action_id'])
    op.drop_constraint('ck_action_status','action_requests',type_='check')
    op.create_check_constraint('ck_action_status','action_requests',"status IN ('awaiting_user','awaiting_staff','approved','ready','rejected','cancelled','expired','stale','executed')")
    op.create_table('action_executions',
        sa.Column('action_id',sa.String(64),sa.ForeignKey('action_requests.id'),primary_key=True),
        sa.Column('user_id',sa.String(128),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('ticket_id',sa.String(40),sa.ForeignKey('tickets.id'),nullable=False),
        sa.Column('event_id',sa.String(64),sa.ForeignKey('ticket_events.id'),nullable=False,unique=True),
        sa.Column('tool',sa.String(40),nullable=False),sa.Column('parameter_hash',sa.String(64),nullable=False),
        sa.Column('draft_hash',sa.String(64),nullable=False),sa.Column('result',sa.JSON(),nullable=False),
        sa.Column('result_hash',sa.String(64),nullable=False),sa.Column('committed_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("tool IN ('ticket_create','ticket_transition')",name='ck_execution_tool'))
    op.create_index('ix_execution_owner_committed','action_executions',['user_id','committed_at'])


def downgrade():
    raise RuntimeError('Archive execution evidence before an explicit downgrade migration')
