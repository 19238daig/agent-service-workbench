"""Safe run/model-call observability, not durable graph execution."""
from alembic import op
import sqlalchemy as sa

revision='0003_run_observability'
down_revision='0002_local_bge'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('execution_runs',sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('request_id',sa.String(32),nullable=False),sa.Column('trace_id',sa.String(32),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),sa.Column('subject_id',sa.String(128),nullable=False),
        sa.Column('status',sa.String(24),nullable=False),sa.Column('snapshot',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('finished_at',sa.DateTime(timezone=True),nullable=True))
    for column in ['request_id','trace_id','session_id','subject_id']:op.create_index('ix_execution_runs_'+column,'execution_runs',[column])
    op.create_table('model_call_records',sa.Column('sequence',sa.Integer(),primary_key=True,autoincrement=True),
        sa.Column('id',sa.String(32),nullable=False,unique=True),
        sa.Column('run_id',sa.String(32),sa.ForeignKey('execution_runs.id',ondelete='CASCADE'),nullable=False),
        sa.Column('request_id',sa.String(32),nullable=False),sa.Column('trace_id',sa.String(32),nullable=False),
        sa.Column('kind',sa.String(24),nullable=False),sa.Column('agent_role',sa.String(64),nullable=False),
        sa.Column('operation',sa.String(80),nullable=False),sa.Column('model',sa.String(160),nullable=False),
        sa.Column('status',sa.String(24),nullable=False),sa.Column('duration_ms',sa.Numeric(14,3),nullable=False),
        sa.Column('payload',sa.JSON(),nullable=False))
    for column in ['run_id','request_id','trace_id']:op.create_index('ix_model_call_records_'+column,'model_call_records',[column])


def downgrade():
    op.drop_table('model_call_records');op.drop_table('execution_runs')
