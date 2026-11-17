from pydantic import BaseModel,Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    trace_id: str
    run_id: str|None=None
    fields: list[dict[str,str]]=Field(default_factory=list)


class ErrorResponse(BaseModel):
    # Keep detail for clients that already expect FastAPI's error envelope.
    detail: str
    error: ErrorDetail


class ApplicationError(Exception):
    def __init__(self,code,message,status_code=500):
        self.code=code;self.message=message;self.status_code=status_code
        super().__init__(code)


def classify_failure(error):
    import asyncio
    import httpx
    name=type(error).__name__
    if isinstance(error,asyncio.CancelledError):return 'RUN_CANCELLED',499,'请求已中断；请核实已有业务结果。'
    if name in {'RateLimitError'} or getattr(error,'status_code',None)==429:return 'MODEL_RATE_LIMITED',429,'模型服务限流，请稍后重试。'
    if isinstance(error,(TimeoutError,httpx.TimeoutException)) or name=='APITimeoutError':return 'MODEL_TIMEOUT',504,'模型调用超时；涉及办理时请先核实结果。'
    if name in {'AuthenticationError','PermissionDeniedError'}:return 'MODEL_ACCESS_DENIED',502,'模型服务认证或访问配置不可用。'
    if isinstance(error,httpx.HTTPError) or name in {'APIConnectionError','APIStatusError','InternalServerError','BadRequestError','NotFoundError'}:return 'MODEL_UNAVAILABLE',502,'上游模型服务暂时不可用。'
    if isinstance(error,ApplicationError):return error.code,error.status_code,error.message
    return 'WORKFLOW_FAILED',500,'执行中断，请核实模型服务与业务结果。'
