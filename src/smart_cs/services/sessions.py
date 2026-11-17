from sqlalchemy import select
from smart_cs.db.models import ChatSession
from smart_cs.repositories.conversations import ConversationRepository
from smart_cs.schemas.errors import ApplicationError


async def owned_session(database, session_id, identity):
    async with database.sessions() as session:
        row=await session.scalar(select(ChatSession).where(ChatSession.id==session_id,ChatSession.user_id==identity.subject_id))
    if row is None:
        raise ApplicationError('SESSION_NOT_FOUND','当前账号未找到此会话。',404)
    return row


async def prepare_session(database, request, identity):
    if request.session_id:
        row=await owned_session(database,request.session_id,identity)
        if row.status != 'active':
            raise ApplicationError('SESSION_ARCHIVED','此会话已归档，请新建对话。',409)
        return row.id
    return await ConversationRepository(database).create(identity.subject_id,request.message[:200])
