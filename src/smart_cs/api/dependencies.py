from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.auth import AuthService, unauthenticated

bearer = HTTPBearer(auto_error=False)


def auth_service(request: Request):
    database = getattr(request.app.state, 'database', None)
    if database is None:
        raise ApplicationError('AUTH_UNAVAILABLE','身份服务暂时不可用。',503)
    return AuthService(database)


async def current_identity(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None or credentials.scheme.lower() != 'bearer':
        raise unauthenticated()
    return await auth_service(request).authenticate(credentials.credentials)


async def administrator(identity=Depends(current_identity)):
    if identity.role != 'admin':
        raise ApplicationError('FORBIDDEN','此操作需要管理员权限。',403)
    return identity
