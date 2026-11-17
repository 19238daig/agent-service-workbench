"""Insert-only synthetic ticket timelines. No existing customer ticket is modified."""
import argparse
import asyncio
from datetime import datetime,timedelta,timezone
import json
from dotenv import load_dotenv
from sqlalchemy import select
from smart_cs.db.models import Ticket,TicketEvent,AuthCredential,User
from smart_cs.settings import ENV_PATH
from smart_cs.db.database import configured_database

SOURCE='synthetic_ticket_fixture_v1'
FIXTURES=[
    ('TK-20261114-9A0001','customer-a','ORDER-A-006','refund','合成退款申请登记：等待核实',2,'created',True),
    ('TK-20261114-9A0002','customer-a','ORDER-A-002','complaint','合成售后投诉：客服处理中',3,'processing',True),
    ('TK-20261114-9B0001','customer-b',None,'general','合成服务记录：尚未分派',1,'created',False),
]


async def seed_ticket_scenarios(database):
    from smart_cs.db.models import Order
    inserted=0;retained=0;fixed=datetime(2026,11,14,6,tzinfo=timezone.utc)
    async with database.sessions.begin() as session:
        accounts=dict((await session.execute(select(AuthCredential.login_name,AuthCredential.user_id).where(AuthCredential.login_name.in_(['customer-a','customer-b','support'])))).all())
        if set(accounts)!={'customer-a','customer-b','support'}:return {'status':'demo_accounts_missing','inserted':0,'retained':0}
        staff=await session.get(User,accounts['support'])
        if staff.role!='support':return {'status':'support_role_unavailable','inserted':0,'retained':0}
        for identifier,login,order_id,kind,title,version,status,assigned in FIXTURES:
            owner=accounts[login];assignee=accounts['support'] if assigned else None
            old=await session.get(Ticket,identifier)
            if old:
                if old.user_id!=owner or old.source!=SOURCE:raise ValueError('Synthetic ticket ID/owner collision')
                retained+=1;continue
            if order_id and not await session.scalar(select(Order.id).where(Order.id==order_id,Order.user_id==owner)):
                raise ValueError('Seed the owned synthetic orders before ticket scenarios')
            session.add(Ticket(id=identifier,user_id=owner,order_id=order_id,type=kind,priority='medium',status=status,
                summary=title,details='合成业务样例；不对应真实客户、真实支付或退款审批。',source=SOURCE,
                created_at=fixed,updated_at=fixed+timedelta(seconds=version-1),version=version,assignee_id=assignee))
            await session.flush()
            events=[TicketEvent(id=identifier+'-event-1',ticket_id=identifier,actor_id=owner,actor_role='customer',event_type='created',
                from_status=None,to_status='created',ticket_version=1,source=SOURCE,note='合成申请登记',created_at=fixed)]
            if assigned:events.append(TicketEvent(id=identifier+'-event-2',ticket_id=identifier,actor_id=owner,actor_role='customer',event_type='assigned',
                from_status='created',to_status='created',ticket_version=2,source=SOURCE,note='合成用户选择客服',assignee_after=assignee,created_at=fixed+timedelta(seconds=1)))
            if status=='processing':events.append(TicketEvent(id=identifier+'-event-3',ticket_id=identifier,actor_id=assignee,actor_role='support',event_type='status_changed',
                from_status='created',to_status='processing',ticket_version=3,source=SOURCE,note='合成客服开始处理',assignee_before=assignee,assignee_after=assignee,created_at=fixed+timedelta(seconds=2)))
            session.add_all(events);inserted+=1
    return {'status':'complete','inserted':inserted,'retained':retained,'synthetic':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['seed']);parser.parse_args();load_dotenv(ENV_PATH)
    async def seed():
        db=configured_database()
        try:return await seed_ticket_scenarios(db)
        finally:await db.close()
    try:print(json.dumps(asyncio.run(seed()),ensure_ascii=False,indent=2));return 0
    except Exception as error:print(json.dumps({'passed':False,'error_type':type(error).__name__}));return 1


if __name__=='__main__':raise SystemExit(main())
