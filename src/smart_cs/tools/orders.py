"""Shared, read-only order tools. Model arguments never choose an owner."""
from pydantic import ValidationError
from smart_cs.schemas.orders import OrderQueryArguments, EligibilityArguments
from smart_cs.schemas.errors import ApplicationError

ORDER_TOOLS=[
    {'name':'order_query','description':'查询当前登录账号的订单事实；提供 order_id 查询单个订单，省略时分页列出本人订单','read_only':True,'input_schema':OrderQueryArguments.model_json_schema()},
    {'name':'order_eligibility','description':'只读核验本人订单的退款申请条件；金额、余额和时限由后端计算，不执行退款或建单','read_only':True,'input_schema':EligibilityArguments.model_json_schema()},
]


async def call_order_tool(service,identity,name,arguments):
    if name not in {'order_query','order_eligibility'}:
        raise ApplicationError('TOOL_FORBIDDEN','此工具未开放直接调用，请通过受控业务流程处理。',403)
    try:
        args=(OrderQueryArguments if name=='order_query' else EligibilityArguments).model_validate(arguments)
    except ValidationError:
        raise ApplicationError('INVALID_TOOL_ARGUMENTS','订单工具参数无效，请检查订单号、分页和两位小数金额。',422) from None
    if name=='order_eligibility':
        return (await service.eligibility(identity,args.order_id,args.requested_amount)).model_dump(mode='json')
    if args.order_id:
        try:return [(await service.get(identity,args.order_id)).model_dump(mode='json')]
        except ApplicationError as error:
            # Preserve the original list-shaped query-tool contract; no oracle.
            if error.code=='ORDER_NOT_FOUND':return []
            raise
    return [row.model_dump(mode='json') for row in (await service.list(identity,args.limit,args.offset)).orders]
