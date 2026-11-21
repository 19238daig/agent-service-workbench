"""Persist user decisions and idempotent receipts for self-service."""
import sqlalchemy as sa
from alembic import op

revision = '0016_self_service'
down_revision = '0015_knowledge_admin'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'self_service_cases',
        sa.Column('user_id', sa.String(128), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('document_id', sa.String(80), sa.ForeignKey('knowledge_documents.id'), nullable=True),
        sa.Column('document_hash', sa.String(64), nullable=True),
        sa.Column('ticket_version', sa.Integer(), nullable=True),
        sa.Column('decision', sa.String(16), nullable=True),
        sa.Column('receipt', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('resolved','handoff')", name='ck_self_service_decision'),
    )


def downgrade():
    raise RuntimeError('Archive self-service decisions before an explicit downgrade')
