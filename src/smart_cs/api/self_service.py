from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from smart_cs.api.dependencies import current_identity
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.self_service import SelfServiceDecision, SelfServiceStart
from smart_cs.services.self_service import SelfService

router = APIRouter(prefix='/api/self-service', tags=['自助处理'])


def service(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    database = getattr(request.app.state, 'database', None)
    if database is None:
        raise ApplicationError('DATABASE_REQUIRED', '自助处理暂时不可用。', 503)
    return SelfService(database)


@router.post('/cases')
async def start(body: SelfServiceStart, identity=Depends(current_identity), flow=Depends(service)):
    return await flow.start(identity, body)


@router.get('/cases/{identifier}')
async def detail(identifier: UUID, identity=Depends(current_identity), flow=Depends(service)):
    return await flow.get(identity, str(identifier))


@router.post('/cases/{identifier}/decision')
async def decide(identifier: UUID, body: SelfServiceDecision, identity=Depends(current_identity), flow=Depends(service)):
    return await flow.decide(identity, str(identifier), body.decision)
