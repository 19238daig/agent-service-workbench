"""Canonical messages and turn/run binding. Existing sessions remain legacy threads."""
from alembic import op
import sqlalchemy as sa

revision='0008_conversation_persistence'
down_revision='0007_ticket_service'
branch_labels=None
depends_on=None


def upgrade():
    with op.batch_alter_table('execution_runs') as batch:
        batch.create_unique_constraint('uq_run_owner_identity',['id','session_id','subject_id'])
    with op.batch_alter_table('chat_sessions') as batch:
        batch.add_column(sa.Column('thread_id',sa.String(128),nullable=True))
        batch.add_column(sa.Column('active_run_id',sa.String(32),nullable=True))
        batch.create_unique_constraint('uq_session_thread_id',['thread_id'])
    with op.batch_alter_table('chat_messages') as batch:
        batch.add_column(sa.Column('run_id',sa.String(32),nullable=True))
        batch.add_column(sa.Column('metadata_json',sa.JSON(),nullable=True))
        batch.create_unique_constraint('uq_message_owner_identity',['id','session_id','user_id'])
        batch.create_foreign_key('fk_message_run_owner','execution_runs',['run_id','session_id','user_id'],['id','session_id','subject_id'])
    # Historical DDL must not depend on future changes to the ORM model.
    op.create_table('chat_turns',
        sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('session_id',sa.String(128),nullable=False),
        sa.Column('user_id',sa.String(128),nullable=False),
        sa.Column('current_run_id',sa.String(32),nullable=False,unique=True),
        sa.Column('user_message_id',sa.String(64),nullable=False,unique=True),
        sa.Column('assistant_message_id',sa.String(64),nullable=True,unique=True),
        sa.Column('graph_version',sa.String(64),nullable=False),
        sa.Column('authorization_role',sa.String(20),nullable=False),
        sa.Column('context_fingerprint',sa.String(64),nullable=False),
        sa.Column('model_fingerprint',sa.String(64),nullable=False),
        sa.Column('status',sa.String(20),nullable=False),
        sa.Column('attempt_count',sa.Integer(),nullable=False),
        sa.Column('error_code',sa.String(64),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['session_id','user_id'],['chat_sessions.id','chat_sessions.user_id'],name='fk_turn_session_owner'),
        sa.ForeignKeyConstraint(['current_run_id','session_id','user_id'],['execution_runs.id','execution_runs.session_id','execution_runs.subject_id'],name='fk_turn_run_owner'),
        sa.ForeignKeyConstraint(['user_message_id','session_id','user_id'],['chat_messages.id','chat_messages.session_id','chat_messages.user_id'],name='fk_turn_user_message'),
        sa.ForeignKeyConstraint(['assistant_message_id','session_id','user_id'],['chat_messages.id','chat_messages.session_id','chat_messages.user_id'],name='fk_turn_assistant_message'),
        sa.CheckConstraint("status IN ('running','completed','blocked','failed','cancelled','interrupted')",name='ck_turn_status'),
        sa.CheckConstraint("authorization_role IN ('customer','support','admin')",name='ck_turn_role'),
        sa.CheckConstraint('attempt_count >= 1',name='ck_turn_attempts'),
        sa.CheckConstraint("(status IN ('completed','blocked') AND assistant_message_id IS NOT NULL) OR (status NOT IN ('completed','blocked') AND assistant_message_id IS NULL)",name='ck_turn_publication'))
    op.create_index('ix_turn_session_created','chat_turns',['session_id','created_at'])


def downgrade():
    op.drop_table('chat_turns')
    with op.batch_alter_table('chat_messages') as batch:
        batch.drop_constraint('fk_message_run_owner',type_='foreignkey')
        batch.drop_constraint('uq_message_owner_identity',type_='unique')
        batch.drop_column('metadata_json');batch.drop_column('run_id')
    with op.batch_alter_table('chat_sessions') as batch:
        batch.drop_constraint('uq_session_thread_id',type_='unique')
        batch.drop_column('active_run_id');batch.drop_column('thread_id')
    with op.batch_alter_table('execution_runs') as batch:batch.drop_constraint('uq_run_owner_identity',type_='unique')
