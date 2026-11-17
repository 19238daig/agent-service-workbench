"""Immutable index document manifests and optional exclusive validity end date."""
from alembic import op
import sqlalchemy as sa

revision='0010_dense_retrieval'
down_revision='0009_knowledge_ingestion'
branch_labels=None
depends_on=None


def upgrade():
    with op.batch_alter_table('knowledge_documents') as batch:
        batch.add_column(sa.Column('effective_until',sa.String(10),nullable=True))
        batch.create_check_constraint('ck_document_valid_interval','effective_until IS NULL OR (length(effective_until) = 10 AND effective_until > effective_from)')
    op.create_table('retrieval_index_documents',
        sa.Column('index_id',sa.String(80),sa.ForeignKey('retrieval_indexes.id',ondelete='CASCADE'),primary_key=True),
        sa.Column('document_id',sa.String(80),sa.ForeignKey('knowledge_documents.id'),primary_key=True),
        sa.Column('chunk_set',sa.String(80),nullable=True),
        sa.Column('chunk_count',sa.Integer(),nullable=False),
        sa.Column('document_fingerprint',sa.String(64),nullable=False),
        sa.Column('chunk_fingerprint',sa.String(64),nullable=False),
        sa.Column('snapshot',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('chunk_count > 0',name='ck_index_document_chunk_count'))


def downgrade():
    op.drop_table('retrieval_index_documents')
    with op.batch_alter_table('knowledge_documents') as batch:
        batch.drop_constraint('ck_document_valid_interval',type_='check')
        batch.drop_column('effective_until')
