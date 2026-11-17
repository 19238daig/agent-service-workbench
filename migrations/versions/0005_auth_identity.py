"""Server-side sessions and document visibility; existing documents stay public."""
from alembic import op
import sqlalchemy as sa
from smart_cs.db.models import AuthCredential, AuthSession, AuthLoginAttempt

revision = '0005_auth_identity'
down_revision = '0004_core_sessions'
branch_labels = None
depends_on = None


def upgrade():
    for table in (AuthCredential.__table__, AuthSession.__table__, AuthLoginAttempt.__table__):
        table.create(op.get_bind())
    with op.batch_alter_table('knowledge_documents') as batch:
        batch.add_column(sa.Column('access_scope', sa.String(16), nullable=False, server_default='public'))
        batch.add_column(sa.Column('owner_id', sa.String(128), nullable=True))
        batch.create_foreign_key('fk_document_owner', 'users', ['owner_id'], ['id'])
        batch.create_check_constraint('ck_document_scope', "access_scope IN ('public','support','admin','private')")
        batch.create_check_constraint('ck_document_owner', "(access_scope = 'private' AND owner_id IS NOT NULL) OR (access_scope <> 'private' AND owner_id IS NULL)")


def downgrade():
    with op.batch_alter_table('knowledge_documents') as batch:
        batch.drop_constraint('ck_document_owner', type_='check')
        batch.drop_constraint('ck_document_scope', type_='check')
        batch.drop_constraint('fk_document_owner', type_='foreignkey')
        batch.drop_column('owner_id')
        batch.drop_column('access_scope')
    for name in ('auth_login_attempts','auth_sessions','auth_credentials'):
        op.drop_table(name)
