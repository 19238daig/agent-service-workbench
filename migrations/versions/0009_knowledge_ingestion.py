"""Additive ingestion receipts and precise source locators; no legacy rewrites."""
from alembic import op
import sqlalchemy as sa

revision='0009_knowledge_ingestion'
down_revision='0008_conversation_persistence'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('knowledge_chunks',sa.Column('locator',sa.JSON(),nullable=True))
    op.create_table('knowledge_ingestions',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('document_id',sa.String(80),sa.ForeignKey('knowledge_documents.id'),nullable=False,unique=True),
        sa.Column('source_key',sa.String(256),nullable=False),
        sa.Column('version',sa.String(32),nullable=False),
        sa.Column('acl_key',sa.String(64),nullable=False),
        sa.Column('artifact_sha256',sa.String(64),nullable=False),
        sa.Column('profile_fingerprint',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('source_key','version','acl_key',name='uq_ingestion_source_version_acl'),
        sa.UniqueConstraint('artifact_sha256','version','acl_key',name='uq_ingestion_artifact_version_acl'),
        sa.CheckConstraint('length(artifact_sha256) = 64',name='ck_ingestion_artifact_hash'),
        sa.CheckConstraint('length(profile_fingerprint) = 64',name='ck_ingestion_profile_hash'))


def downgrade():
    op.drop_table('knowledge_ingestions')
    op.drop_column('knowledge_chunks','locator')
