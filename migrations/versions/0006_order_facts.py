"""Nullable after-sales facts; never infer historical refunds or delivery times."""
from alembic import op
import sqlalchemy as sa

revision='0006_order_facts'
down_revision='0005_auth_identity'
branch_labels=None
depends_on=None

CHECKS=[
    ('ck_order_refunded_amount','refunded_amount IS NULL OR (refunded_amount >= 0 AND refunded_amount <= paid_amount)'),
    ('ck_order_reserved_amount','refund_reserved_amount IS NULL OR (refund_reserved_amount >= 0 AND refund_reserved_amount <= paid_amount)'),
    ('ck_order_refund_total','refunded_amount IS NULL OR refund_reserved_amount IS NULL OR refunded_amount + refund_reserved_amount <= paid_amount'),
    ('ck_order_shipped_time','shipped_at IS NULL OR shipped_at >= paid_at'),
    ('ck_order_delivery_time',"delivered_at IS NULL OR (shipped_at IS NOT NULL AND delivered_at >= shipped_at AND status = 'delivered')"),
    ('ck_order_return_policy',"return_policy IN ('standard_7d','excluded','unknown')"),
    ('ck_order_return_condition',"return_condition IN ('intact','not_intact','unknown')"),
    ('ck_order_fact_version','fact_version >= 1'),
]


def upgrade():
    with op.batch_alter_table('orders') as batch:
        for name in ['shipped_at','delivered_at']:
            batch.add_column(sa.Column(name,sa.DateTime(timezone=True),nullable=True))
        for name in ['refunded_amount','refund_reserved_amount']:
            batch.add_column(sa.Column(name,sa.Numeric(12,2),nullable=True))
        for name in ['return_policy','return_condition']:
            batch.add_column(sa.Column(name,sa.String(24),nullable=False,server_default='unknown'))
        batch.add_column(sa.Column('fact_version',sa.Integer(),nullable=False,server_default='1'))
        for name,expression in CHECKS:batch.create_check_constraint(name,expression)


def downgrade():
    with op.batch_alter_table('orders') as batch:
        for name,_ in reversed(CHECKS):batch.drop_constraint(name,type_='check')
        for name in ['fact_version','return_condition','return_policy','refund_reserved_amount','refunded_amount','delivered_at','shipped_at']:
            batch.drop_column(name)
