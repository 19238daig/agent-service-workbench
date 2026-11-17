"""Insert-only, manifest-checked business scenarios; never reassign existing orders."""
import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from dotenv import load_dotenv
from sqlalchemy import select
from pydantic import BaseModel,ConfigDict
from typing import Literal
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import Order,AuthCredential
from smart_cs.schemas.orders import OrderFacts,Decision,Money
from smart_cs.services.orders import POLICY_VERSION,evaluate_refund

FIXTURE_ROOT=PROJECT_ROOT/'data/order-fixtures-v1'


class Scenario(BaseModel):
    model_config=ConfigDict(extra='forbid')
    owner_login: Literal['customer-a','customer-b']
    case: str
    facts: OrderFacts
    expected_decision: Decision
    expected_maximum: Money


def load_order_scenarios(root=FIXTURE_ROOT):
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    raw=(root/'orders.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['sha256'] or manifest['policy_version']!=POLICY_VERSION:
        raise ValueError('Order fixture manifest mismatch')
    rows=[Scenario.model_validate(row) for row in json.loads(raw)]
    if len(rows)!=manifest['records'] or len({row.facts.order_id for row in rows})!=len(rows):raise ValueError('Invalid order fixture identities')
    instant=datetime.fromisoformat(manifest['reference_time'])
    for row in rows:
        result=evaluate_refund(row.facts,instant)
        if result.decision!=row.expected_decision or result.maximum_refund_amount!=row.expected_maximum:
            raise ValueError('Order scenario expected outcome mismatch')
    return manifest,rows


async def seed_order_scenarios(database):
    manifest,rows=load_order_scenarios()
    inserted=0;retained=0
    async with database.sessions.begin() as session:
        owners=dict((await session.execute(select(AuthCredential.login_name,AuthCredential.user_id).where(
            AuthCredential.login_name.in_(['customer-a','customer-b'])))).all())
        if set(owners)!={'customer-a','customer-b'}:
            return {'inserted':0,'retained':0,'status':'demo_accounts_missing','fixture_version':manifest['version']}
        for scenario in rows:
            facts=scenario.facts;owner=owners[scenario.owner_login]
            existing=await session.get(Order,facts.order_id)
            if existing:
                if existing.user_id!=owner or existing.source!='synthetic_order_fixture_v1':
                    raise ValueError('Fixture order ID collision; refusing to change ownership or overwrite data')
                retained+=1;continue
            values=facts.model_dump(exclude={'order_id','user_id'})
            for key in ['paid_amount','refunded_amount','refund_reserved_amount']:
                values[key]=Decimal(values[key]) if values[key] is not None else None
            session.add(Order(id=facts.order_id,user_id=owner,**values));inserted+=1
    return {'inserted':inserted,'retained':retained,'status':'complete','fixture_version':manifest['version'],
            'reference_time':manifest['reference_time'],'synthetic':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['check','seed']);args=parser.parse_args()
    load_dotenv(ENV_PATH)
    try:
        manifest,rows=load_order_scenarios()
        if args.action=='check':result={'passed':True,**manifest}
        else:
            async def seed():
                db=configured_database()
                if db is None:raise ValueError('Database is not configured')
                try:return await seed_order_scenarios(db)
                finally:await db.close()
            result=asyncio.run(seed())
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as error:
        print(json.dumps({'passed':False,'error_type':type(error).__name__}));return 1


if __name__=='__main__':raise SystemExit(main())
