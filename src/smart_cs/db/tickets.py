"""Deprecated offline fixture facade. Live APIs use TicketService; Agent gets a reader.

This compatibility adapter keeps early deterministic evaluations reproducible.
It has no HTTP exposure, requires an explicit owner and reuses the transactional
repository. It must never be injected into the live graph.
"""
from pydantic import ValidationError
from smart_cs.repositories.tickets import PostgresTicketRepository
from smart_cs.schemas.tickets import TicketRegistration,TicketView
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.tickets import TRANSITIONS


class DatabaseTicketStore:
    def __init__(self,database):self.repository=PostgresTicketRepository(database)

    def identity(self,user_id):
        if not isinstance(user_id,str) or not user_id.strip() or len(user_id)>128:raise ValueError('An explicit fixture owner is required')
        return IdentityContext(subject_id=user_id)

    async def create(self,ticket_type,priority,summary,details,user_id,order_id=None):
        identity=self.identity(user_id)
        try:
            body=TicketRegistration(type=ticket_type,priority=priority,summary=summary,details=details,order_id=order_id,confirm_registration=True)
            async with self.repository.transaction() as unit:row=await unit.create(identity,body,'offline_fixture',fixture_user=True)
            return TicketView.model_validate(row).model_dump(mode='json')
        except (ValidationError,ApplicationError) as error:raise ValueError('Invalid offline ticket fixture') from error

    async def query(self,ticket_id,user_id=None):
        row=await self.repository.get(self.identity(user_id),ticket_id)
        return TicketView.model_validate(row).model_dump(mode='json') if row else None

    async def query_by_user(self,user_id,limit=50,offset=0):
        rows,_=await self.repository.list(self.identity(user_id),limit,offset)
        return [TicketView.model_validate(row).model_dump(mode='json') for row in rows]

    async def update_status(self,ticket_id,status,user_id=None):
        identity=self.identity(user_id)
        try:
            async with self.repository.transaction() as unit:
                row=await unit.get(identity,ticket_id)
                if status not in TRANSITIONS[row.status]:raise ValueError('Invalid fixture transition')
                row=await unit.change(row,identity,row.version,'status_changed','offline fixture transition',source='offline_fixture',target_status=status)
            return TicketView.model_validate(row).model_dump(mode='json')
        except ApplicationError as error:
            if error.status_code==404:return None
            raise ValueError('Offline fixture update failed') from error
