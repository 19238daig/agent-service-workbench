import os
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.identity import IdentityContext


def local_identity(user_id):
    if os.getenv('APP_MODE','local_demo')!='local_demo':
        raise ApplicationError('LOCAL_DEMO_ONLY','当前版本尚未提供登录认证，仅开放本地演示。',503)
    if not isinstance(user_id,str) or not user_id.strip() or len(user_id.strip())>128:
        raise ApplicationError('INVALID_IDENTITY','工作区标识无效。',400)
    return IdentityContext(subject_id=user_id.strip())
