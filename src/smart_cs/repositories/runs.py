import copy
from typing import Protocol
from sqlalchemy import select,func
from smart_cs.db.models import ExecutionRun,ModelCallRecord,RunControl,RunEvent,utcnow


class RunRepository(Protocol):
    backend: str
    async def start(self,record:dict):...
    async def finish(self,record:dict,calls:list[dict]):...
    async def get(self,run_id:str,subject_id:str)->dict|None:...


class MemoryRunRepository:
    backend='process_memory'
    def __init__(self):self.records={}
    async def start(self,record):
        # Explicit bounded adapter for offline tests, not durable production storage.
        if len(self.records)>=1000:
            finished=next((key for key,value in self.records.items() if value['status']!='running'),None)
            if finished:self.records.pop(finished)
            else:raise RuntimeError('Offline run capacity reached')
        self.records[record['run_id']]=copy.deepcopy({**record,'model_calls':[]})
    async def finish(self,record,calls):self.records[record['run_id']]=copy.deepcopy({**record,'model_calls':calls})
    async def get(self,run_id,subject_id):
        result=self.records.get(run_id)
        return copy.deepcopy(result) if result and result['identity']['subject_id']==subject_id else None


class DatabaseRunRepository:
    backend='database'
    def __init__(self,database):self.database=database
    async def start(self,record):
        async with self.database.sessions.begin() as session:
            await self.start_in_session(session,record)
    async def start_in_session(self,session,record):
        session.add(ExecutionRun(id=record['run_id'],request_id=record['request_id'],trace_id=record['trace_id'],
            session_id=record['session_id'],subject_id=record['identity']['subject_id'],status=record['status'],snapshot=dict(record)))
        await session.flush()
    async def finish(self,record,calls):
        async with self.database.sessions.begin() as session:
            await self.finish_in_session(session,record,calls)
    async def finish_in_session(self,session,record,calls):
        run=await session.get(ExecutionRun,record['run_id'])
        if run is None:raise RuntimeError('Run was not initialized')
        run.status=record['status'];run.snapshot=dict(record);run.finished_at=utcnow()
        await self.calls_in_session(session,calls)
    async def calls_in_session(self,session,calls):
        for call in calls:
            existing=await session.scalar(select(ModelCallRecord).where(ModelCallRecord.id==call['id']))
            if existing:
                if existing.run_id!=call['run_id'] or existing.payload!=call:raise RuntimeError('Model call binding changed')
                continue
            session.add(ModelCallRecord(id=call['id'],run_id=call['run_id'],request_id=call['request_id'],trace_id=call['trace_id'],
                kind=call['kind'],agent_role=call['agent_role'],operation=call['operation'],model=call['model'],
                status=call['status'],duration_ms=call['duration_ms'],payload=call))
    async def get(self,run_id,subject_id):
        async with self.database.sessions() as session:
            run=await session.scalar(select(ExecutionRun).where(ExecutionRun.id==run_id,ExecutionRun.subject_id==subject_id))
            if run is None:return None
            calls=(await session.scalars(select(ModelCallRecord).where(ModelCallRecord.run_id==run_id).order_by(ModelCallRecord.sequence))).all()
            result={**run.snapshot,'model_calls':[call.payload for call in calls]}
            control=await session.get(RunControl,run_id)
            if control and run.status in {'queued','running','interrupted'}:
                from smart_cs.observability.context import usage_summary
                started=await session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id==run_id,RunEvent.event_type=='model_call_started'))
                inflight=max(0,started-len(calls))
                usage={**usage_summary(result['model_calls']),'observed_started_calls':started,'inflight_calls':inflight,
                    'recording_incomplete':run.status=='interrupted' or inflight>0}
                if usage['recording_incomplete']:usage['status']='unknown'
                result['usage']=usage
            return result
