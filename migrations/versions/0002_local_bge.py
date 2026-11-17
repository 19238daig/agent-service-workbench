"""Versioned local BGE indexes and original-document citation locators."""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR

revision = '0002_local_bge'
down_revision = '0001_foundation'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('knowledge_documents',sa.Column('file_path',sa.String(1024),nullable=True))
    op.add_column('knowledge_documents',sa.Column('mime_type',sa.String(64),nullable=False,server_default='text/markdown'))
    op.add_column('knowledge_documents',sa.Column('metadata_json',sa.JSON(),nullable=True))
    for name,kind in [('source_page',sa.Integer()),('chunk_set',sa.String(80)),('heading',sa.String(500)),('kind',sa.String(24)),('anchor',sa.String(180)),('token_count',sa.Integer())]:
        op.add_column('knowledge_chunks',sa.Column(name,kind,nullable=True))
    with op.batch_alter_table('knowledge_chunks') as batch:
        batch.drop_constraint('uq_chunk_position',type_='unique')
        batch.create_unique_constraint('uq_chunk_set_position',['document_id','chunk_set','ordinal'])
    op.create_table('retrieval_indexes',sa.Column('id',sa.String(80),primary_key=True),sa.Column('active',sa.Boolean(),nullable=False),sa.Column('model_identity',sa.JSON(),nullable=False),sa.Column('configuration',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('uq_active_retrieval_index','retrieval_indexes',['active'],unique=True,postgresql_where=sa.text('active'),sqlite_where=sa.text('active = 1'))
    op.create_table('knowledge_embeddings',sa.Column('index_id',sa.String(80),sa.ForeignKey('retrieval_indexes.id',ondelete='CASCADE'),primary_key=True),sa.Column('chunk_id',sa.String(100),sa.ForeignKey('knowledge_chunks.id',ondelete='CASCADE'),primary_key=True),sa.Column('embedding',VECTOR(1024).with_variant(sa.JSON(),'sqlite'),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM knowledge_chunks WHERE chunk_set IS NOT NULL')).scalar():
        raise RuntimeError('Restore the baseline database for a pre-BGE rollback; chunk versions cannot be collapsed safely')
    op.drop_table('knowledge_embeddings')
    op.drop_table('retrieval_indexes')
    with op.batch_alter_table('knowledge_chunks') as batch:
        batch.drop_constraint('uq_chunk_set_position',type_='unique')
        batch.create_unique_constraint('uq_chunk_position',['document_id','ordinal'])
    for name in ['token_count','anchor','kind','heading','chunk_set','source_page']:
        op.drop_column('knowledge_chunks',name)
    for name in ['metadata_json','mime_type','file_path']:
        op.drop_column('knowledge_documents',name)
