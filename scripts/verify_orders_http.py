"""Local authenticated read-only order acceptance; --chat invokes real models."""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import time
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from authenticated_client import authenticated_client
from verify_core_schema import fingerprints


async def business_snapshot():
    db=configured_database()
    try:
        all_tables=await fingerprints(db)
        return {name:all_tables[name] for name in ['orders','tickets','ticket_events']}
    finally:await db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--chat',action='store_true');args=parser.parse_args()
    load_dotenv(ENV_PATH)
    report={'passed':False,'real_api':True,'real_model':args.chat}
    try:
        before=asyncio.run(business_snapshot())
        with ExitStack() as stack:
            clients=[stack.enter_context(httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=240)) for _ in range(3)]
            a,b,admin=clients
            for _ in range(30):
                try:
                    if a.get('/health/live').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(1)
            else:raise RuntimeError('API startup timeout')
            user=stack.enter_context(authenticated_client(a,'customer-a'))
            stack.enter_context(authenticated_client(b,'customer-b'))
            stack.enter_context(authenticated_client(admin,'admin'))
            orders=a.get('/api/orders',params={'user_id':'forged-b'}).json()
            assert orders['total']==12 and len(orders['orders'])==12
            assert b.get('/api/orders').json()['total']==2
            facts=a.get('/api/orders/ORDER-A-006').json()['order']
            assert facts['paid_amount']=='500.00' and facts['refunded_amount']=='100.01' and facts['refund_reserved_amount']=='49.99'
            result=a.get('/api/orders/ORDER-A-006/refund-eligibility').json()
            assert result['can_apply'] and result['maximum_refund_amount']=='350.00'
            assert b.get('/api/orders/ORDER-A-006/refund-eligibility',params={'user_id':user['id']}).status_code==404
            expired=a.get('/api/orders/ORDER-A-003/refund-eligibility',params={'as_of':'2026-11-06T00:00:00Z'}).json()
            assert expired['reason_codes']==['WINDOW_EXPIRED']
            assert a.post('/api/tools/call',json={'name':'order_eligibility','arguments':{'order_id':'ORDER-A-006','paid_amount':'999999.00'}}).status_code==422
            assert a.post('/api/tools/call',json={'name':'ticket_create','arguments':{}}).status_code==403
            unknown=admin.get('/api/orders/DEMO-000001')
            assert unknown.status_code==404  # Admin does not inherit fixture-customer ownership.
            if args.chat:
                response=a.post('/api/chat/stream',json={'message':'请只核验我的订单 ORDER-A-006 的退款申请条件和当前可申请金额上限，不要创建工单。'})
                assert response.status_code==200
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                final=next(event for event in events if event['type']=='complete')
                assert final['compliance_passed'] and '350.00' in final['response'] and '500.00' in final['response']
                step=next(item for item in final['execution_trace'] if item['node']=='ticket_handler')
                assert step['summary']['action']=='refund_eligibility' and step['summary']['maximum_refund_amount']=='350.00'
                record=a.get('/api/runs/'+final['run_id']).json()
                assert record['identity']['subject_id']==user['id']
                report.update(run_id=final['run_id'],nodes=[step['node'] for step in final['execution_trace']],usage=record['usage'])
            report.update(orders_a=12,orders_b=2,paid_amount='500.00',refunded_amount='100.01',reserved_amount='49.99',maximum_refund_amount='350.00',
                cross_owner_denied=True,fabricated_amount_rejected=True,expired_order_denied=True,write_tool_denied=True)
        after=asyncio.run(business_snapshot())
        assert before==after
        report.update(passed=True,business_facts_unchanged=True)
    except Exception as error:report['error_type']=type(error).__name__
    output=PROJECT_ROOT/'.local/orders';output.mkdir(parents=True,exist_ok=True)
    (output/'live-http-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
