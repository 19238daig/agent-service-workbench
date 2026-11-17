from contextlib import nullcontext
from time import perf_counter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from smart_cs.schemas.errors import ApplicationError,ErrorDetail,ErrorResponse
from smart_cs.observability.context import RequestCorrelation,request_context,correlation
from smart_cs.tracing.otel_config import get_tracer


def problem(code,message,status,fields=None):
    ids=correlation()
    body=ErrorResponse(detail=message,error=ErrorDetail(code=code,message=message,request_id=ids.request_id,
        trace_id=ids.trace_id,run_id=ids.run_id,fields=fields or []))
    return JSONResponse(body.model_dump(),status_code=status)


class CorrelationMiddleware:
    """Pure ASGI middleware keeps context alive until the last SSE body frame."""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        tracer=get_tracer();started=False;begin=perf_counter()
        manager=tracer.start_as_current_span('http.request',record_exception=False,set_status_on_exception=False) if tracer else nullcontext(None)
        with manager as span:
            ids=RequestCorrelation()
            if span:
                context=span.get_span_context()
                if context.is_valid:ids.trace_id=f'{context.trace_id:032x}'
                span.set_attribute('app.request_id',ids.request_id);span.set_attribute('http.request.method',scope['method'])
            token=request_context.set(ids)
            scope.setdefault('state',{})['correlation']=ids
            async def send_correlated(message):
                nonlocal started
                if message['type']=='http.response.start':
                    started=True;message['headers']=list(message.get('headers',[]))+[(b'x-request-id',ids.request_id.encode()),(b'x-trace-id',ids.trace_id.encode())]
                    if scope.get('path','').startswith('/api/'):
                        message['headers']=[h for h in message['headers'] if h[0].lower()!=b'cache-control']+[(b'cache-control',b'no-store')]
                    if message['status']==401:message['headers'].append((b'www-authenticate',b'Bearer'))
                    if ids.run_id:message['headers'].append((b'x-run-id',ids.run_id.encode()))
                    if span:span.set_attribute('http.response.status_code',message['status'])
                await send(message)
            try:await self.app(scope,receive,send_correlated)
            except Exception:
                if started:raise
                await problem('INTERNAL_ERROR','服务处理异常，请通过请求编号查询。',500)(scope,receive,send_correlated)
            finally:
                if span:span.set_attribute('http.duration_ms',round((perf_counter()-begin)*1000,3))
                request_context.reset(token)


def install_error_handlers(app):
    @app.exception_handler(ApplicationError)
    async def app_error(request,error):return problem(error.code,error.message,error.status_code)
    @app.exception_handler(HTTPException)
    async def http_error(request,error):
        codes={400:'INVALID_REQUEST',401:'UNAUTHENTICATED',403:'FORBIDDEN',404:'NOT_FOUND',409:'CONFLICT',429:'RATE_LIMITED',503:'SERVICE_UNAVAILABLE'}
        # HTTPException details are application-owned text, not raw provider payloads.
        message=error.detail if isinstance(error.detail,str) else '请求未能完成。'
        return problem(codes.get(error.status_code,'HTTP_ERROR'),message,error.status_code)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error):
        fields=[{'field':'.'.join(str(x)[:64] for x in item['loc']),'type':item['type']} for item in error.errors()]
        return problem('VALIDATION_ERROR','请求字段无效，请检查输入。',422,fields)
