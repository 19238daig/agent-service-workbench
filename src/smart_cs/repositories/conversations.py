"""Low-level message repository; online fenced execution lives in persistence/."""
from uuid import uuid4
from sqlalchemy import select,update
from smart_cs.db.models import ChatSession,ChatMessage,utcnow


class ConversationRepository:
    def __init__(self,database):self.database=database

    async def create(self,user_id,title='新对话',session_id=None):
        if not title.strip() or len(title)>200:raise ValueError('Invalid conversation title')
        identifier=session_id or str(uuid4())
        if not identifier or len(identifier)>128:raise ValueError('Invalid session identifier')
        async with self.database.sessions.begin() as session:
            row=ChatSession(id=identifier,user_id=user_id,title=title,source='local_demo')
            session.add(row);await session.flush()
        return identifier

    async def append(self,session_id,user_id,role,content,message_id=None):
        if role not in {'user','assistant'} or not isinstance(content,str) or not content.strip():raise ValueError('Invalid product message')
        async with self.database.sessions.begin() as session:
            # UPDATE locks one PostgreSQL session row. Counter and message commit together.
            sequence=await session.scalar(update(ChatSession).where(ChatSession.id==session_id,ChatSession.user_id==user_id,
                ChatSession.status=='active').values(next_sequence=ChatSession.next_sequence+1,version=ChatSession.version+1,
                updated_at=utcnow()).returning(ChatSession.next_sequence))
            if sequence is None:return None
            row=ChatMessage(id=message_id or uuid4().hex,session_id=session_id,user_id=user_id,sequence=sequence-1,role=role,content=content,source='local_demo')
            session.add(row);await session.flush()
            return {'id':row.id,'sequence':row.sequence,'role':row.role,'content':row.content}

    async def history(self,session_id,user_id):
        async with self.database.sessions() as session:
            owner=await session.scalar(select(ChatSession.id).where(ChatSession.id==session_id,ChatSession.user_id==user_id))
            if owner is None:return None
            messages=(await session.scalars(select(ChatMessage).where(ChatMessage.session_id==session_id,ChatMessage.user_id==user_id).order_by(ChatMessage.sequence))).all()
            return [{'id':m.id,'sequence':m.sequence,'role':m.role,'content':m.content} for m in messages]
