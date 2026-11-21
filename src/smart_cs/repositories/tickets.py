"""SQL repository; one transaction commits a ticket mutation and its event."""
from contextlib import asynccontextmanager
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import select,update,func,or_
from smart_cs.db.models import Ticket,TicketEvent,User,AuthCredential,Order,utcnow
from smart_cs.schemas.errors import ApplicationError


def visibility(identity):
    own=Ticket.user_id==identity.subject_id
    return or_(own,Ticket.assignee_id==identity.subject_id) if identity.role in {'support','admin'} else own


def missing():return ApplicationError('TICKET_NOT_FOUND','当前账号未找到此工单。',404)


class TicketUnitOfWork:
    def __init__(self,session):self.session=session

    async def get(self,identity,ticket_id):
        row=await self.session.scalar(select(Ticket).where(Ticket.id==ticket_id,visibility(identity)).with_for_update())
        if row is None:raise missing()
        return row

    async def create(self,owner,body,source,fixture_user=False,*,action_id=None,requested_amount=None):
        if fixture_user:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            insert=pg_insert if self.session.bind.dialect.name=='postgresql' else sqlite_insert
            await self.session.execute(insert(User).values(id=owner.subject_id,display_name='离线样例用户',role='customer',source='local_demo').on_conflict_do_nothing(index_elements=['id']))
        if body.order_id:
            order=await self.session.scalar(select(Order.id).where(Order.id==body.order_id,Order.user_id==owner.subject_id))
            if order is None:raise ApplicationError('ORDER_NOT_FOUND','当前账号未找到此订单。',404)
        now=utcnow()
        row=Ticket(id=f'TK-{now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")}-{uuid4().hex[:6].upper()}',
            user_id=owner.subject_id,order_id=body.order_id,type=body.type,priority=body.priority,
            summary=body.summary,details=body.details,requested_amount=requested_amount,status='created',version=1,source=source,created_at=now,updated_at=now)
        self.session.add(row);await self.session.flush()
        registration_note = {'manual_api':'人工登记服务申请', 'self_service':'用户提交自助服务记录'}.get(source, '离线合成样例')
        self.session.add(TicketEvent(id=uuid4().hex,ticket_id=row.id,actor_id=owner.subject_id,actor_role=owner.role if owner.authenticated else None,
            action_id=action_id,event_type='created',from_status=None,to_status='created',ticket_version=1,source=source,
            note='已确认动作登记申请' if action_id else registration_note,created_at=now))
        await self.session.flush()
        return row

    async def assignable_staff(self,identifier):
        return await self.session.scalar(select(User.id).join(AuthCredential,AuthCredential.user_id==User.id).where(
            User.id==identifier,User.role.in_(['support','admin']),AuthCredential.enabled.is_(True)))

    async def change(self,row,actor,expected_version,event_type,note,source='manual_api',target_status=None,change_assignee=False,assignee_id=None,action_id=None):
        previous=row.status;before_assignee=row.assignee_id;version=row.version
        if version!=expected_version:raise ApplicationError('TICKET_VERSION_CONFLICT','工单已更新，请重新读取后提交。',409)
        target=target_status or previous;after_assignee=assignee_id if change_assignee else before_assignee;now=utcnow()
        changed=await self.session.scalar(update(Ticket).where(Ticket.id==row.id,Ticket.version==expected_version).values(
            status=target,assignee_id=after_assignee,version=version+1,updated_at=now).returning(Ticket.id).execution_options(synchronize_session=False))
        if changed is None:raise ApplicationError('TICKET_VERSION_CONFLICT','工单已更新，请重新读取后提交。',409)
        self.session.add(TicketEvent(id=uuid4().hex,ticket_id=row.id,actor_id=actor.subject_id,actor_role=actor.role if actor.authenticated else None,
            action_id=action_id,event_type=event_type,from_status=previous,to_status=target,ticket_version=version+1,source=source,note=note,
            assignee_before=before_assignee,assignee_after=after_assignee,created_at=now))
        await self.session.flush();await self.session.refresh(row)
        return row


class PostgresTicketRepository:
    """Production PostgreSQL repository; SQLite is an explicit offline test adapter."""
    def __init__(self,database):self.database=database

    @asynccontextmanager
    async def transaction(self):
        async with self.database.sessions.begin() as session:yield TicketUnitOfWork(session)

    async def get(self,identity,ticket_id):
        async with self.database.sessions() as session:
            return await session.scalar(select(Ticket).where(Ticket.id==ticket_id,visibility(identity)))

    async def list(self,identity,limit=50,offset=0,assigned=False,status=None):
        scope=Ticket.assignee_id==identity.subject_id if assigned else Ticket.user_id==identity.subject_id
        conditions=[scope]+([Ticket.status==status] if status else [])
        async with self.database.sessions() as session:
            total=await session.scalar(select(func.count()).select_from(Ticket).where(*conditions))
            rows=(await session.scalars(select(Ticket).where(*conditions).order_by(Ticket.updated_at.desc(),Ticket.id).offset(offset).limit(limit))).all()
        return rows,total

    async def events(self,identity,ticket_id,limit,offset):
        async with self.database.sessions() as session:
            conditions=[TicketEvent.ticket_id==ticket_id,visibility(identity)]
            total=await session.scalar(select(func.count()).select_from(TicketEvent).join(Ticket,Ticket.id==TicketEvent.ticket_id).where(*conditions))
            rows=(await session.scalars(select(TicketEvent).join(Ticket,Ticket.id==TicketEvent.ticket_id).where(*conditions).order_by(TicketEvent.ticket_version.asc().nulls_first(),TicketEvent.created_at,TicketEvent.id).offset(offset).limit(limit))).all()
        return rows,total

    async def staff(self):
        async with self.database.sessions() as session:
            rows=(await session.scalars(select(User).join(AuthCredential,AuthCredential.user_id==User.id).where(
                User.role.in_(['support','admin']),AuthCredential.enabled.is_(True)).order_by(User.display_name,User.id).limit(100))).all()
        return [{'id':row.id,'display_name':row.display_name,'role':row.role} for row in rows]
