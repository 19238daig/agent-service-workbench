"""Atomic operator publication and index activation audit."""
from alembic import op
import sqlalchemy as sa

revision = '0015_knowledge_admin'
down_revision = '0014_durable_runs'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('knowledge_admin_events',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('user_id', sa.String(128), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('resource_id', sa.String(128), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('document_published','document_withdrawn','index_activated')", name='ck_knowledge_admin_kind'))
    op.create_index('ix_knowledge_admin_created', 'knowledge_admin_events', ['created_at'])


def downgrade():
    raise RuntimeError('Archive operator audit before an explicit downgrade')
