"""Read-only repository: every query requires a server-provided owner ID."""
from datetime import timezone
from sqlalchemy import select, func
from smart_cs.db.models import Order
from smart_cs.schemas.orders import OrderFacts


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def order_facts(row):
    return OrderFacts(order_id=row.id,user_id=row.user_id,product_name=row.product_name,
        paid_amount=format(row.paid_amount,'.2f'),currency=row.currency,status=row.status,
        paid_at=aware(row.paid_at),shipped_at=aware(row.shipped_at) if row.shipped_at else None,
        delivered_at=aware(row.delivered_at) if row.delivered_at else None,
        refunded_amount=format(row.refunded_amount,'.2f') if row.refunded_amount is not None else None,
        refund_reserved_amount=format(row.refund_reserved_amount,'.2f') if row.refund_reserved_amount is not None else None,
        return_policy=row.return_policy,return_condition=row.return_condition,fact_version=row.fact_version,source=row.source)


class OrderRepository:
    def __init__(self,database):self.database=database

    async def get(self,order_id,owner_id):
        async with self.database.sessions() as session:
            row=await session.scalar(select(Order).where(Order.id==order_id,Order.user_id==owner_id))
            return order_facts(row) if row else None

    async def list(self,owner_id,limit=50,offset=0):
        async with self.database.sessions() as session:
            total=await session.scalar(select(func.count()).select_from(Order).where(Order.user_id==owner_id))
            rows=(await session.scalars(select(Order).where(Order.user_id==owner_id).order_by(Order.id).offset(offset).limit(limit))).all()
            return [order_facts(row) for row in rows],total
