"""Restricted-schema child process: intentionally exit only after a real commit."""
import asyncio
from datetime import datetime
import os
import re
import sys
from sqlalchemy import select,text
from smart_cs.db.database import Database
from smart_cs.db.models import User
from smart_cs.schemas.identity import IdentityContext
from smart_cs.services.execution import ActionExecutionService


async def main():
    schema=os.environ.get('AUTH_VERIFY_SCHEMA','')
    if not re.fullmatch(r'foundation_verify_[a-f0-9]{12}',schema):raise RuntimeError('Isolated verification schema required')
    db=Database(os.environ['DATABASE_URL'],schema=schema)
    try:
        async with db.sessions() as s:
            if await s.scalar(text('SELECT current_user'))!=schema:raise RuntimeError('Restricted test role required')
            if await s.scalar(text("SELECT has_table_privilege(current_user,'public.users','INSERT,UPDATE,DELETE,TRUNCATE')")):raise RuntimeError('Public writes must be forbidden')
            user=await s.get(User,sys.argv[2])
        identity=IdentityContext(subject_id=user.id,role=user.role,authenticated=True,mode='authenticated_session')
        async def fault(stage):
            if stage=='after_commit':os._exit(77)
        now=datetime.fromisoformat(os.environ['EXECUTION_VERIFY_NOW'])
        await ActionExecutionService(db,clock=lambda:now,fault=fault).execute(identity,sys.argv[1],sys.argv[3])
    finally:await db.close()
    raise RuntimeError('Expected post-commit process exit')


if __name__=='__main__':asyncio.run(main())
