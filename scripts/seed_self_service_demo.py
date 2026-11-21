"""Load one clearly labelled fictional product manual into a local demo database."""
import asyncio
import os

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

from smart_cs.db.database import configured_database
from smart_cs.ingestion.pipeline import IngestionPipeline
from smart_cs.settings import ENV_PATH, PROJECT_ROOT


async def main():
    load_dotenv(ENV_PATH)
    url = os.getenv('DATABASE_URL', '')
    if not url or os.getenv('APP_MODE', 'local_demo') != 'local_demo' or make_url(url).host not in {'127.0.0.1', 'localhost'}:
        raise SystemExit('Demo manual requires a local demo PostgreSQL database.')
    path = PROJECT_ROOT / '.local' / 'self-service' / 'demo-x1.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'DEMO-X1 商品说明书（虚构商品，仅供演示）\n\n'
        '包装清单：主机一件、快速指南一份。\n'
        '查找型号：查看包装标签上的型号字段，确认标记为 DEMO-X1。\n'
        '如果电子版已解决说明书缺失问题，可返回工单确认已解决。\n',
        encoding='utf-8',
    )
    db = configured_database()
    try:
        document = await IngestionPipeline(db).ingest(path, {
            'source_key': 'manual:demo-x1', 'title': 'DEMO-X1 商品说明书（演示）',
            'version': '1', 'domain': 'helpdesk_reference', 'access_scope': 'public',
            'effective_from': '2020-01-01', 'source_type': 'synthetic_self_service',
            'publisher': '项目演示资料', 'license': '仅用于项目演示',
        }, publish=True)
        print('已准备 DEMO-X1 说明书，状态：' + document['status'])
    finally:
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
