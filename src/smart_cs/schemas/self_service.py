from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from smart_cs.schemas.orders import OrderId
from smart_cs.schemas.tickets import TicketId

ModelNumber = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]


class SelfServiceStart(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    request_id: UUID
    kind: Literal['manual', 'damage', 'compensation', 'other']
    issue: str = Field(min_length=2, max_length=500)
    model_number: ModelNumber | None = None
    order_id: OrderId | None = None
    ticket_id: TicketId | None = None

    @model_validator(mode='after')
    def check_model(self):
        if self.kind == 'manual' and not self.model_number:
            raise ValueError('查找说明书需要商品型号。')
        return self


class SelfServiceDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')

    decision: Literal['resolved', 'handoff']
