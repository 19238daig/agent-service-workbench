from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Money = Annotated[str, StringConstraints(pattern=r'^(?:0|[1-9][0-9]{0,9})\.[0-9]{2}$')]
OrderId = Annotated[str, StringConstraints(strip_whitespace=True,min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')]
OrderStatus = Literal['pending_shipment','shipped','delivered','cancelled']
Decision = Literal['allowed','denied','requires_review','needs_information']


class OrderFacts(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid')
    order_id: str
    user_id: str
    product_name: str
    paid_amount: Money
    currency: Literal['CNY']
    status: OrderStatus
    paid_at: datetime
    shipped_at: datetime | None
    delivered_at: datetime | None
    refunded_amount: Money | None
    refund_reserved_amount: Money | None
    return_policy: Literal['standard_7d','excluded','unknown']
    return_condition: Literal['intact','not_intact','unknown']
    fact_version: int
    source: str


class OrderList(BaseModel):
    demo_only: bool=True
    orders: list[OrderFacts]
    total: int
    limit: int
    offset: int
    has_more: bool


class RefundEligibility(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid')
    order_id: str
    decision: Decision
    can_apply: bool
    reason_codes: list[str]
    missing_facts: list[str]=Field(default_factory=list)
    maximum_refund_amount: Money
    remaining_paid_amount: Money | None
    requested_amount: Money | None
    currency: Literal['CNY']='CNY'
    evaluated_at: datetime
    application_deadline: datetime | None
    policy_version: str
    fact_version: int
    facts: OrderFacts
    scope: Literal['synthetic_shop_application_check_only']='synthetic_shop_application_check_only'
    disclaimer: str='仅核验虚构店铺的申请登记条件，不代表退款审批、实际支付或法律结论。'


class OrderQueryArguments(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    order_id: OrderId | None=None
    limit: int=Field(default=50,ge=1,le=100)
    offset: int=Field(default=0,ge=0)
    # Deprecated compatibility field; deliberately never used as authority.
    user_id: str | None=Field(default=None,max_length=128,deprecated=True)


class EligibilityArguments(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    order_id: OrderId
    requested_amount: Money | None=None


class OrderToolCall(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name: str=Field(min_length=1,max_length=64)
    arguments: dict=Field(default_factory=dict)
