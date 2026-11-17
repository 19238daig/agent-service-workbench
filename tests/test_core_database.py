import tempfile
from pathlib import Path
from decimal import Decimal
import unittest
from sqlalchemy import select,delete,func
from sqlalchemy.exc import IntegrityError
from smart_cs.db.database import Database
from smart_cs.db.models import User,Order,Ticket,TicketEvent,ChatSession,ChatMessage,KnowledgeDocument,KnowledgeChunk,utcnow
from smart_cs.db.seed import seed_database
from smart_cs.repositories.conversations import ConversationRepository


class CoreDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.url='sqlite+aiosqlite:///'+str(Path(self.temp.name)/'core.db').replace('\\','/')
        self.db=Database(self.url);await self.db.initialize(create_schema=True)
        async with self.db.sessions.begin() as session:
            session.add_all([User(id=key,display_name='合成验证用户',role='customer',source='test_fixture') for key in ['a','b']])
        self.repo=ConversationRepository(self.db)

    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()

    async def reject(self,row):
        with self.assertRaises(IntegrityError):
            async with self.db.sessions.begin() as session:session.add(row);await session.flush()

    def order(self,**overrides):
        return Order(**{'id':'order','user_id':'a','product_name':'合成商品','paid_amount':Decimal('99.10'),'currency':'CNY','status':'delivered','paid_at':utcnow(),'source':'test_fixture',**overrides})

    async def test_money_currency_status_and_user_foreign_key(self):
        for change in [{'paid_amount':Decimal('-1')},{'currency':'USD'},{'status':'invented'},{'user_id':'missing'},{'paid_amount':Decimal('10000000000.00')}]:
            with self.subTest(change=change):await self.reject(self.order(**change))
        async with self.db.sessions.begin() as session:session.add(self.order())
        async with self.db.sessions() as session:self.assertEqual((await session.get(Order,'order')).paid_amount,Decimal('99.10'))

    async def test_ticket_cannot_reference_another_owners_order(self):
        async with self.db.sessions.begin() as session:session.add(self.order())
        await self.reject(Ticket(id='bad',user_id='b',order_id='order',type='refund',priority='medium',status='created',summary='非法跨归属',details=''))

    async def test_session_owner_message_sequence_role_and_content_constraints(self):
        identifier=await self.repo.create('a')
        first=await self.repo.append(identifier,'a','user','合成消息')
        for change in [{'user_id':'b'},{'session_id':'missing'},{'sequence':first['sequence']},{'sequence':0},{'role':'tool'},{'content':'   '}]:
            values={'id':'bad','session_id':identifier,'user_id':'a','sequence':2,'role':'user','content':'合成验证',**change}
            with self.subTest(change=change):await self.reject(ChatMessage(**values))
        self.assertIsNone(await self.repo.append(identifier,'b','user','越归属'))
        self.assertIsNone(await self.repo.history(identifier,'b'))

    async def test_message_insert_failure_rolls_back_sequence_counter(self):
        identifier=await self.repo.create('a')
        await self.repo.append(identifier,'a','user','第一条',message_id='same')
        with self.assertRaises(IntegrityError):await self.repo.append(identifier,'a','assistant','重复主键',message_id='same')
        async with self.db.sessions() as session:
            row=await session.get(ChatSession,identifier);self.assertEqual((row.next_sequence,row.version),(2,2))
        self.assertEqual((await self.repo.append(identifier,'a','assistant','第二条'))['sequence'],2)

    async def test_new_engine_can_read_messages_and_session_delete_cascades(self):
        identifier=await self.repo.create('a');await self.repo.append(identifier,'a','user','重连验证')
        await self.db.close();self.db=Database(self.url);self.repo=ConversationRepository(self.db)
        self.assertEqual((await self.repo.history(identifier,'a'))[0]['content'],'重连验证')
        async with self.db.sessions.begin() as session:await session.execute(delete(ChatSession).where(ChatSession.id==identifier))
        async with self.db.sessions() as session:self.assertEqual(await session.scalar(select(func.count()).select_from(ChatMessage)),0)

    async def test_seed_does_not_reset_existing_session_or_append_sequence(self):
        await seed_database(self.db)
        await self.repo.append('fixture-session-001','demo-user-001','user','追加的合成验证消息')
        before=await self.repo.history('fixture-session-001','demo-user-001')
        await seed_database(self.db)
        self.assertEqual(before,await self.repo.history('fixture-session-001','demo-user-001'))
        summary=await self.db.summary()
        self.assertEqual(summary['counts']['chat_sessions'],20);self.assertEqual(summary['counts']['chat_messages'],41)

    async def test_legacy_null_chunk_set_still_has_unique_positions(self):
        async with self.db.sessions.begin() as session:
            session.add(KnowledgeDocument(id='doc',source='test.txt',title='合成',version='v1',domain='test',status='published',content='content',content_hash='a'*64,effective_from='2026-11-14'))
        async with self.db.sessions.begin() as session:
            session.add(KnowledgeChunk(id='c1',document_id='doc',ordinal=0,chunk_set=None,content='content',content_hash='a'*64))
        await self.reject(KnowledgeChunk(id='c2',document_id='doc',ordinal=0,chunk_set=None,content='content',content_hash='a'*64))

if __name__=='__main__':unittest.main()
