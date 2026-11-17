import os
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from smart_cs.api.dependencies import auth_service, current_identity
from smart_cs.db.models import User
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.auth import ACCESS_SECONDS, REFRESH_SECONDS

router=APIRouter(prefix='/api/auth',tags=['身份认证'])
REFRESH_COOKIE='smartcs_refresh'
CSRF_COOKIE='smartcs_csrf'
ALLOWED_ORIGINS={'http://127.0.0.1:8000','http://localhost:8000','http://127.0.0.1:5173','http://localhost:5173'}


def allowed_origins():
    from urllib.parse import urlsplit
    result=set(ALLOWED_ORIGINS)
    for raw in os.getenv('APP_ORIGINS','').split(','):
        value=raw.strip()
        if not value:continue
        parsed=urlsplit(value)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment or '*' in value:
            raise ValueError('APP_ORIGINS must contain exact http(s) origins')
        if parsed.port is not None and not 1<=parsed.port<=65535:raise ValueError('Invalid origin port')
        result.add(value)
    return result


class LoginRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    login_name: str=Field(min_length=3,max_length=64)
    password: str=Field(min_length=1,max_length=128)


def origin_check(request):
    origin=request.headers.get('origin')
    # Missing Origin is allowed for non-browser clients, which still need CSRF.
    if origin and origin not in allowed_origins():
        raise ApplicationError('CSRF_REJECTED','请求来源不被允许。',403)


def user_view(user):
    return {'id':user.id,'display_name':user.display_name,'role':user.role}


def issue(response, result):
    access,refresh,csrf,user=result
    secure=os.getenv('AUTH_COOKIE_SECURE','false').lower()=='true'
    response.headers['Cache-Control']='no-store'
    response.set_cookie(REFRESH_COOKIE,refresh,max_age=REFRESH_SECONDS,httponly=True,secure=secure,samesite='strict',path='/api/auth')
    response.set_cookie(CSRF_COOKIE,csrf,max_age=REFRESH_SECONDS,httponly=False,secure=secure,samesite='strict',path='/')
    return {'access_token':access,'token_type':'bearer','expires_in':ACCESS_SECONDS,'user':user_view(user)}


@router.post('/login')
async def login(body: LoginRequest, request: Request, response: Response, service=Depends(auth_service)):
    origin_check(request)
    return issue(response,await service.login(body.login_name,body.password,request.client.host if request.client else 'unknown'))


@router.post('/refresh')
async def refresh(request: Request, response: Response, service=Depends(auth_service)):
    origin_check(request)
    return issue(response,await service.refresh(request.cookies.get(REFRESH_COOKIE),request.headers.get('x-csrf-token')))


@router.post('/logout',status_code=204)
async def logout(response: Response, identity=Depends(current_identity), service=Depends(auth_service)):
    await service.logout(identity)
    response.headers['Cache-Control']='no-store'
    response.delete_cookie(REFRESH_COOKIE,path='/api/auth')
    response.delete_cookie(CSRF_COOKIE,path='/')


@router.get('/me')
async def me(response: Response, identity=Depends(current_identity), service=Depends(auth_service)):
    response.headers['Cache-Control']='no-store'
    async with service.database.sessions() as session:
        return user_view(await session.get(User,identity.subject_id))
