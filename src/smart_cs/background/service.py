"""Durable administrative jobs. PostgreSQL is the outbox and result backend."""
import hashlib
import asyncio
import json
from datetime import datetime,timezone,timedelta
from uuid import uuid4
from sqlalchemy import select,text,or_,and_
from sqlalchemy.exc import IntegrityError
from smart_cs.db.models import BackgroundJob as Job,BackgroundJobEvent as Event,User,AuthCredential,RetrievalIndex
from smart_cs.schemas.jobs import JobRequest
from smart_cs.schemas.errors import ApplicationError
from smart_cs.background.files import JobFiles
from smart_cs.settings import PROJECT_ROOT

def now():return datetime.now(timezone.utc)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def expired(value):return value is None or value.replace(tzinfo=timezone.utc)<=now()
def record(session,job,kind,**payload):
    job.updated_at=now()
    session.add(Event(id=uuid4().hex,job_id=job.id,version=job.version,event_type=kind,payload=payload))
def view(job):
    return {k:getattr(job,k) for k in ['id','kind','payload_hash','binding','status','attempt','max_attempts','dispatch_count','next_dispatch_at','worker','stage','progress','total','result','error_code','version','created_at','updated_at','heartbeat_at','lease_until']}

async def authorize(session,user_id,*,worker=False,lock=False):
    if worker and session.bind.dialect.name=='postgresql':
        allowed=await session.scalar(text('SELECT smart_cs_worker_admin(:actor)'),{'actor':user_id})
    else:
        query=select(User.id).join(AuthCredential).where(User.id==user_id,User.role=='admin',AuthCredential.enabled.is_(True))
        if lock:query=query.with_for_update(read=True)
        allowed=await session.scalar(query)
    if not allowed:raise ApplicationError('JOB_ADMIN_REVOKED','后台任务需要有效的管理员身份。',403)

class JobService:
    def __init__(self,database,root=PROJECT_ROOT):self.db=database;self.root=root;self.files=JobFiles(root)
    async def submit(self,identity,request):
        if not identity.authenticated or identity.role!='admin':raise ApplicationError('FORBIDDEN','需要管理员权限。',403)
        request=JobRequest.model_validate(request);payload=request.work.model_dump(mode='json');sha=digest(payload)
        async def previous(s):
            job=await s.scalar(select(Job).where(Job.user_id==identity.subject_id,Job.idempotency_key==request.idempotency_key))
            if job and job.payload_hash!=sha:raise ApplicationError('JOB_IDEMPOTENCY_CONFLICT','同一幂等键不能更换任务参数。',409)
            return job
        try:
            async with self.db.sessions.begin() as s:
                await authorize(s,identity.subject_id)
                old=await previous(s)
                if old:return view(old)
                binding={}
                if payload['kind']=='ingest':
                    await asyncio.to_thread(self.files.verify,payload['artifact_sha256'],payload['extension'])
                    binding={'artifact_sha256':payload['artifact_sha256']}
                if payload['kind']=='retrieval_evaluation':
                    from smart_cs.evaluation.assets import ROOT
                    index=await s.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
                    if index is None:raise ApplicationError('ACTIVE_VECTOR_INDEX_MISSING','没有活动索引。',409)
                    binding={'index_id':index.id,'dataset_sha256':hashlib.sha256((ROOT/'manifest.json').read_bytes()).hexdigest()}
                job=Job(id=uuid4().hex,user_id=identity.subject_id,idempotency_key=request.idempotency_key,kind=payload['kind'],payload=payload,payload_hash=sha,binding=binding,version=1)
                s.add(job);await s.flush();record(s,job,'queued')
                return view(job)
        except IntegrityError:
            async with self.db.sessions() as s:
                old=await previous(s)
                if old:return view(old)
            raise
    async def owned(self,s,identity,identifier,lock=False):
        await authorize(s,identity.subject_id)
        q=select(Job).where(Job.id==identifier,Job.user_id==identity.subject_id)
        if lock:q=q.with_for_update()
        job=await s.scalar(q)
        if job is None:raise ApplicationError('JOB_NOT_FOUND','任务不存在。',404)
        return job
    async def get(self,identity,identifier):
        async with self.db.sessions() as s:return view(await self.owned(s,identity,identifier))
    async def listing(self,identity):
        async with self.db.sessions() as s:
            await authorize(s,identity.subject_id)
            return [view(j) for j in (await s.scalars(select(Job).where(Job.user_id==identity.subject_id).order_by(Job.created_at.desc()).limit(100))).all()]
    async def events(self,identity,identifier,after=0):
        async with self.db.sessions() as s:
            await self.owned(s,identity,identifier)
            return [{'version':e.version,'type':e.event_type,'payload':e.payload,'created_at':e.created_at} for e in (await s.scalars(select(Event).where(Event.job_id==identifier,Event.version>after).order_by(Event.version).limit(100))).all()]
    async def cancel(self,identity,identifier):
        async with self.db.sessions.begin() as s:
            job=await self.owned(s,identity,identifier,True)
            if job.status in {'queued','retry_wait','running'}:
                job.status='cancel_requested' if job.status=='running' else 'cancelled';job.version+=1;record(s,job,job.status)
            return view(job)
    async def retry(self,identity,identifier,payload_hash):
        async with self.db.sessions.begin() as s:
            job=await self.owned(s,identity,identifier,True)
            if job.payload_hash!=payload_hash:raise ApplicationError('JOB_PAYLOAD_CHANGED','参数指纹不符。',409)
            if job.status!='failed' or job.attempt>=job.max_attempts:raise ApplicationError('JOB_RETRY_UNAVAILABLE','状态不允许重试或已耗尽三次执行预算。',409)
            job.status='queued';job.next_dispatch_at=now();job.error_code=None;job.version+=1;record(s,job,'manual_retry')
            return view(job)

async def dispatch_once(database,publish):
    """DB rows survive failed publishing, broker loss and dispatcher restarts."""
    async with database.sessions() as s:
        ready=or_(Job.status.in_(['queued','retry_wait']),and_(Job.status.in_(['running','cancel_requested']),or_(Job.lease_until.is_(None),Job.lease_until<=now())))
        jobs=list((await s.scalars(select(Job.id).where(ready,Job.next_dispatch_at<=now()).order_by(Job.next_dispatch_at).limit(50))).all())
    sent=0
    for identifier in jobs:
        async with database.sessions.begin() as s:
            job=await s.scalar(select(Job).where(Job.id==identifier).with_for_update(skip_locked=True))
            if job is None or not expired(job.next_dispatch_at):continue
            if job.status in {'running','cancel_requested'} and not expired(job.lease_until):continue
            # Throttle before delivery. A crash here delays, rather than loses, delivery.
            job.next_dispatch_at=now()+timedelta(seconds=15);job.dispatch_count+=1
        try:await publish(identifier);sent+=1
        except Exception:pass  # Never persist broker URLs or provider exception text.
    return sent
