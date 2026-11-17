"""Versioned synthetic shop policy. Reads facts; never writes or refunds money."""
from datetime import timedelta
from decimal import Decimal
from pydantic import TypeAdapter, ValidationError
from smart_cs.db.models import utcnow
from smart_cs.repositories.orders import aware
from smart_cs.schemas.orders import Money, OrderId, RefundEligibility, OrderList
from smart_cs.schemas.errors import ApplicationError

POLICY_VERSION='synthetic-shop-refund-v1'
REASONS={
    'BEFORE_SHIPMENT':'订单尚未发货，可登记退款申请',
    'WITHIN_RETURN_WINDOW':'已签收且在演示退货时限内',
    'ORDER_CANCELLED':'订单已取消，不接受重复的退款申请',
    'NO_REFUND_BALANCE':'实付扣除已退款与已占用金额后没有可申请余额',
    'IN_TRANSIT_REVIEW':'订单运输中，需要人工核实退回及交付情况',
    'EXCLUDED_POLICY_REVIEW':'此商品不适用演示的无理由退货规则，需要人工核实其他申请依据',
    'WINDOW_EXPIRED':'已超出演示的七日退货申请时限',
    'CONDITION_NOT_INTACT':'商品状态不满足演示的完好条件',
    'FACTS_MISSING':'缺少必要的订单事实，不能自动判定',
    'FACTS_INCONSISTENT':'订单事实时间或金额不一致，需要核实',
    'REQUEST_EXCEEDS_MAXIMUM':'申请金额超过后端计算的当前上限',
}


class OrderService:
    def __init__(self,repository,clock=utcnow):self.repository=repository;self.clock=clock

    async def list(self,identity,limit=50,offset=0):
        if type(limit) is not int or type(offset) is not int or not 1<=limit<=100 or offset<0:
            raise ApplicationError('INVALID_PAGINATION','分页参数无效。',422)
        rows,total=await self.repository.list(identity.subject_id,limit,offset)
        return OrderList(orders=rows,total=total,limit=limit,offset=offset,has_more=offset+len(rows)<total)

    async def get(self,identity,order_id):
        try:order_id=TypeAdapter(OrderId).validate_python(order_id)
        except ValidationError:raise ApplicationError('INVALID_ORDER_ID','订单号格式无效。',422) from None
        row=await self.repository.get(order_id,identity.subject_id)
        if row is None:raise ApplicationError('ORDER_NOT_FOUND','当前账号未找到此订单。',404)
        return row

    async def eligibility(self,identity,order_id,requested_amount=None):
        if requested_amount is not None:
            try:requested_amount=TypeAdapter(Money).validate_python(requested_amount)
            except ValidationError:raise ApplicationError('INVALID_AMOUNT','金额必须为正的两位小数字符串。',422) from None
            if Decimal(requested_amount)<=0:raise ApplicationError('INVALID_AMOUNT','申请金额必须大于零。',422)
        facts=await self.get(identity,order_id)
        return evaluate_refund(facts,aware(self.clock()),requested_amount)


def evaluate_refund(facts,now,requested=None):
    """Pure rule function with an injected server clock for reproducible boundaries."""
    now=aware(now);decision='needs_information';codes=[];missing=[];maximum=Decimal('0.00');remaining=None;deadline=None
    paid=Decimal(facts.paid_amount)
    if facts.refunded_amount is None:missing.append('refunded_amount')
    if facts.refund_reserved_amount is None:missing.append('refund_reserved_amount')
    if facts.return_policy=='unknown':missing.append('return_policy')
    if facts.status in {'shipped','delivered'} and facts.shipped_at is None:missing.append('shipped_at')
    if facts.status=='delivered':
        if facts.delivered_at is None:missing.append('delivered_at')
        if facts.return_condition=='unknown':missing.append('return_condition')
    if facts.refunded_amount is not None and facts.refund_reserved_amount is not None:
        remaining=paid-Decimal(facts.refunded_amount)-Decimal(facts.refund_reserved_amount)
    inconsistent=(facts.paid_at>now or any(t and (t<facts.paid_at or t>now) for t in [facts.shipped_at,facts.delivered_at])
        or (facts.shipped_at and facts.delivered_at and facts.delivered_at<facts.shipped_at)
        or (facts.status=='pending_shipment' and (facts.shipped_at or facts.delivered_at))
        or (remaining is not None and remaining<0))
    if facts.delivered_at:deadline=facts.delivered_at+timedelta(days=7)
    if inconsistent:codes=['FACTS_INCONSISTENT']
    elif facts.status=='cancelled':decision='denied';codes=['ORDER_CANCELLED']
    elif missing:codes=['FACTS_MISSING']
    elif remaining<=0:decision='denied';codes=['NO_REFUND_BALANCE']
    elif facts.status=='pending_shipment':decision='allowed';codes=['BEFORE_SHIPMENT'];maximum=remaining
    elif facts.status=='shipped':decision='requires_review';codes=['IN_TRANSIT_REVIEW']
    elif facts.return_policy=='excluded':decision='requires_review';codes=['EXCLUDED_POLICY_REVIEW']
    elif now>deadline:decision='denied';codes=['WINDOW_EXPIRED']
    elif facts.return_condition!='intact':decision='denied';codes=['CONDITION_NOT_INTACT']
    else:decision='allowed';codes=['WITHIN_RETURN_WINDOW'];maximum=remaining
    if requested is not None and decision=='allowed' and Decimal(requested)>maximum:
        decision='denied';codes=['REQUEST_EXCEEDS_MAXIMUM']
    return RefundEligibility(order_id=facts.order_id,decision=decision,can_apply=decision=='allowed',reason_codes=codes,
        missing_facts=missing,maximum_refund_amount=format(maximum,'.2f'),remaining_paid_amount=format(remaining,'.2f') if remaining is not None and remaining>=0 else None,
        requested_amount=requested,evaluated_at=now,application_deadline=deadline,policy_version=POLICY_VERSION,fact_version=facts.fact_version,facts=facts)


def format_order(facts,eligibility=None):
    # Only database facts and deterministic rules enter this text, never model prose.
    status={'pending_shipment':'待发货','shipped':'运输中','delivered':'已签收','cancelled':'已取消'}[facts.status]
    lines=[f'订单号：{facts.order_id}',f'商品：{facts.product_name}',f'状态：{status}',f'实付金额：{facts.paid_amount} 元（{facts.currency}）',f'支付时间：{facts.paid_at.isoformat()}']
    if facts.delivered_at:lines.append(f'签收时间：{facts.delivered_at.isoformat()}')
    if eligibility:
        labels={'allowed':'可登记申请','denied':'当前条件不满足','requires_review':'需要人工核实','needs_information':'需要补充订单事实'}
        lines.extend([f'申请条件：{labels[eligibility.decision]}',*[REASONS[code] for code in eligibility.reason_codes],
            f'当前规则下可申请金额上限：{eligibility.maximum_refund_amount} 元'])
        if eligibility.missing_facts:lines.append('缺少事实：'+', '.join(eligibility.missing_facts))
        lines.append(f'核验时间：{eligibility.evaluated_at.isoformat()}；规则：{eligibility.policy_version}')
    lines.append('以上仅为虚构店铺的订单事实与申请条件查询，不代表退款审批通过或实际退款到账。')
    return '\n\n'.join(lines)
