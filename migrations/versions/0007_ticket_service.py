"""Ticket assignment and versioned audit. Legacy chronology stays explicitly unknown."""
from alembic import op
import sqlalchemy as sa

revision='0007_ticket_service'
down_revision='0006_order_facts'
branch_labels=None
depends_on=None


def upgrade():
    with op.batch_alter_table('tickets') as batch:
        batch.add_column(sa.Column('assignee_id',sa.String(128),nullable=True))
        batch.create_foreign_key('fk_ticket_assignee','users',['assignee_id'],['id'])
        batch.create_index('ix_ticket_assignee_status',['assignee_id','status','updated_at'])
    with op.batch_alter_table('ticket_events') as batch:
        batch.add_column(sa.Column('ticket_version',sa.Integer(),nullable=True))
        batch.add_column(sa.Column('actor_role',sa.String(20),nullable=True))
        batch.add_column(sa.Column('source',sa.String(32),nullable=True))
        batch.add_column(sa.Column('note',sa.String(500),nullable=False,server_default=''))
        for name in ['assignee_before','assignee_after']:
            batch.add_column(sa.Column(name,sa.String(128),nullable=True))
            batch.create_foreign_key('fk_event_'+name,'users',[name],['id'])
        batch.create_check_constraint('ck_event_ticket_version','ticket_version IS NULL OR ticket_version >= 1')
        batch.create_check_constraint('ck_event_actor_role',"actor_role IS NULL OR actor_role IN ('customer','support','admin')")
        batch.create_unique_constraint('uq_event_ticket_version',['ticket_id','ticket_version'])


def downgrade():
    with op.batch_alter_table('ticket_events') as batch:
        batch.drop_constraint('uq_event_ticket_version',type_='unique')
        batch.drop_constraint('ck_event_actor_role',type_='check')
        batch.drop_constraint('ck_event_ticket_version',type_='check')
        for name in ['assignee_before','assignee_after']:batch.drop_constraint('fk_event_'+name,type_='foreignkey')
        for name in ['ticket_version','actor_role','source','note','assignee_before','assignee_after']:batch.drop_column(name)
    with op.batch_alter_table('tickets') as batch:
        batch.drop_index('ix_ticket_assignee_status')
        batch.drop_constraint('fk_ticket_assignee',type_='foreignkey')
        batch.drop_column('assignee_id')
