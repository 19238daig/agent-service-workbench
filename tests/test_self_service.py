import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from smart_cs.db.database import Database
from smart_cs.db.models import AuthCredential, KnowledgeDocument, Ticket, TicketEvent, User
from smart_cs.ingestion.pipeline import IngestionPipeline
from smart_cs.repositories.tickets import PostgresTicketRepository
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.self_service import SelfServiceStart
from smart_cs.schemas.tickets import TicketRegistration
from smart_cs.services.self_service import SelfService
from smart_cs.services.tickets import TicketService


def actor(name, role='customer'):
    return IdentityContext(subject_id=name, role=role, authenticated=True, mode='authenticated_session')


class SelfServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.schema = os.getenv('AUTH_VERIFY_SCHEMA')
        self.db = Database(os.environ['DATABASE_URL'] if self.schema else 'sqlite+aiosqlite:///' + (self.root/'tests.db').as_posix(), schema=self.schema)
        self.addAsyncCleanup(self.db.close)
        await self.db.initialize(create_schema=True)
        suffix = uuid4().hex[:8]
        self.owner, self.other, self.staff = actor('a-' + suffix), actor('b-' + suffix), actor('s-' + suffix, 'support')
        async with self.db.sessions.begin() as session:
            for person in (self.owner, self.other, self.staff):
                session.add(User(id=person.subject_id, display_name=person.subject_id, role=person.role))
            await session.flush()
            session.add(AuthCredential(user_id=self.staff.subject_id, login_name=self.staff.subject_id, password_hash='fixture'))
        self.service = SelfService(self.db, self.root)
        self.tickets = TicketService(PostgresTicketRepository(self.db))
        self.model = 'FIX-' + suffix.upper()
        path = self.root/'manual.txt'
        path.write_text('Sample manual. Exact model: ' + self.model, encoding='utf-8')
        self.manual = await IngestionPipeline(self.db, root=self.root).ingest(path, {
            'source_key': 'manual:' + self.model.lower(), 'title': 'Verified fixture manual', 'version': '1',
            'domain': 'helpdesk_reference', 'access_scope': 'public', 'effective_from': '2020-01-01',
            'source_type': 'synthetic_fixture', 'publisher': 'test fixture', 'license': 'fixture',
        }, publish=True)

    def body(self, **overrides):
        return SelfServiceStart(**{
            'request_id': uuid4(), 'kind': 'manual', 'model_number': self.model,
            'issue': '包装里缺少说明书。', **overrides,
        })

    async def count(self):
        async with self.db.sessions() as session:
            return await session.scalar(select(func.count()).select_from(Ticket).where(Ticket.user_id == self.owner.subject_id))

    async def test_exact_manual_and_one_confirmed_ticket(self):
        body = self.body(model_number=self.model.lower())
        quoted = await self.service.start(self.owner, body)
        self.assertEqual(quoted, await self.service.start(self.owner, body))
        self.assertEqual(quoted['status'], 'manual_available')
        self.assertEqual(quoted['manual']['version'], '1')
        self.assertEqual(await self.count(), 0)
        if self.schema:
            left, right = await asyncio.gather(*(self.service.decide(self.owner, quoted['id'], 'resolved') for _ in range(2)))
        else:
            left = await self.service.decide(self.owner, quoted['id'], 'resolved')
            right = await self.service.decide(self.owner, quoted['id'], 'resolved')
        self.assertEqual(left, right)
        self.assertEqual(await self.count(), 1)
        identifier = left['receipt']['ticket_id']
        saved = await self.tickets.get(self.owner, identifier)
        self.assertEqual((saved.status, saved.version, saved.assignee_id), ('resolved', 2, None))
        events = await self.tickets.events(self.owner, identifier)
        self.assertEqual([e['event_type'] for e in events['events']], ['created', 'self_service_resolved'])

    async def test_unmatched_or_high_risk_cannot_resolve_and_support_gets_handoff(self):
        for body in (self.body(model_number=self.model + 'X'), self.body(issue='缺说明书，商品破损，需要赔偿。')):
            offer = await self.service.start(self.owner, body)
            self.assertEqual(offer['status'], 'handoff_available')
            with self.assertRaises(ApplicationError) as denied:
                await self.service.decide(self.owner, offer['id'], 'resolved')
            self.assertEqual(denied.exception.code, 'MANUAL_RESOLUTION_UNAVAILABLE')
        self.assertEqual(await self.count(), 0)
        body = self.body(kind='damage', model_number=None, issue='收到时外包装破损。')
        flow = await self.service.start(self.owner, body)
        receipt = (await self.service.decide(self.owner, flow['id'], 'handoff'))['receipt']
        self.assertEqual(receipt['ticket_status'], 'escalated')
        self.assertTrue(receipt['assigned_to_staff'])
        async with self.db.sessions() as session:
            event = await session.scalar(select(TicketEvent).where(TicketEvent.ticket_id == receipt['ticket_id'],
                                                                    TicketEvent.event_type == 'self_service_handoff'))
        self.assertIn('待办：核对订单、商品外观和收货情况', event.note)
        self.assertIn('收到时外包装破损', event.note)

    async def test_revoked_manual_and_wrong_owner_do_not_write(self):
        flow = await self.service.start(self.owner, self.body())
        with self.assertRaises(ApplicationError) as hidden:
            await self.service.decide(self.other, flow['id'], 'resolved')
        self.assertEqual(hidden.exception.status_code, 404)
        async with self.db.sessions.begin() as session:
            document = await session.get(KnowledgeDocument, self.manual['document_id'])
            document.status = 'withdrawn'
        with self.assertRaises(ApplicationError) as stale:
            await self.service.decide(self.owner, flow['id'], 'resolved')
        self.assertEqual(stale.exception.code, 'MANUAL_OFFER_STALE')
        self.assertEqual(await self.count(), 0)

    async def test_existing_ticket_is_reused_and_changed_version_is_rejected(self):
        ticket = await self.tickets.register(self.owner, TicketRegistration(type='general', summary='缺少说明书', confirm_registration=True))
        with self.assertRaises(ApplicationError) as hidden:
            await self.service.start(self.other, self.body(ticket_id=ticket.ticket_id))
        self.assertEqual(hidden.exception.status_code, 404)
        offer = await self.service.start(self.owner, self.body(ticket_id=ticket.ticket_id))
        async with self.db.sessions.begin() as session:
            saved = await session.get(Ticket, ticket.ticket_id)
            saved.version += 1
        with self.assertRaises(ApplicationError) as stale:
            await self.service.decide(self.owner, offer['id'], 'resolved')
        self.assertEqual(stale.exception.code, 'TICKET_VERSION_CONFLICT')
        updated = await self.service.start(self.owner, self.body(ticket_id=ticket.ticket_id))
        result = await self.service.decide(self.owner, updated['id'], 'resolved')
        self.assertEqual(result['receipt']['ticket_id'], ticket.ticket_id)
        self.assertEqual(await self.count(), 1)
