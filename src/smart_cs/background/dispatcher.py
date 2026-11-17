"""Independent durable outbox polling; a busy worker cannot delay recovery."""
import asyncio
from smart_cs.background.celery_app import execute
from smart_cs.background.service import dispatch_once
from smart_cs.db.database import configured_database

async def main():
    db=configured_database()
    async def publish(identifier):await asyncio.to_thread(execute.apply_async,args=[identifier],retry=False)
    try:
        while True:
            try:await dispatch_once(db,publish)
            except Exception:pass
            await asyncio.sleep(3)
    finally:await db.close()

if __name__=='__main__':asyncio.run(main())
