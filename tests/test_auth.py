"""Real authentication and direct-API isolation; no identity dependency overrides."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import timedelta
from sqlalchemy import select, update
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
import smart_cs.api.main as main
from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.db.database import Database
from smart_cs.db.models import AuthCredential, AuthSession, User, Order, KnowledgeDocument, ChatSession, utcnow
from smart_cs.db.tickets import DatabaseTicketStore
from smart_cs.memory.short_term import ShortTermMemory
from smart_cs.services.auth import AuthService, hash_password, verify_password, digest
from smart_cs.services.access import document_visibility, principal_context
from smart_cs.schemas.identity import IdentityContext

PASSWORD='Test-only-password-2026!'
PASSWORD_HASH=hash_password(PASSWORD)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(main.app.dependency_overrides,{})
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        (self.root/'data/source.txt').write_text('restricted document contents',encoding='utf-8')
        self.url='sqlite+aiosqlite:///'+str(self.root/'auth.db').replace('\\','/')
        self.schema=None
        # Only the explicit PostgreSQL verification runner sets these, in tests/.
        if os.getenv('AUTH_VERIFY_SCHEMA'):
            self.url=os.environ['DATABASE_URL'];self.schema=os.environ['AUTH_VERIFY_SCHEMA']
        async def seed():
            db=self.db()
            try:
                if self.schema:
                    from smart_cs.db.models import Base
                    self.assertEqual(db.engine.get_execution_options().get('schema_translate_map'),{None:self.schema})
                    async with db.engine.begin() as connection:await connection.run_sync(Base.metadata.drop_all)
                await db.initialize(create_schema=True)
                if self.schema:
                    from smart_cs.persistence.postgres import prepare_checkpoint_store
                    await prepare_checkpoint_store(db)
                async with db.sessions.begin() as session:
                    for name,role in [('alice','customer'),('bob','customer'),('agent','support'),('admin','admin')]:
                        session.add(User(id=name,display_name=name,role=role,source='auth_test'))
                    await session.flush()
                    for name in ['alice','bob','agent','admin']:
                        session.add(AuthCredential(user_id=name,login_name=name,password_hash=PASSWORD_HASH))
                    for name in ['alice','bob']:
                        session.add(Order(id='order-'+name,user_id=name,product_name='测试订单',paid_amount=1,currency='CNY',status='delivered',paid_at=utcnow()))
                    for identifier,scope,owner in [('public','public',None),('alice-private','private','alice'),('bob-private','private','bob'),('staff','support',None),('management','admin',None)]:
                        session.add(KnowledgeDocument(id=identifier,source=identifier+'.txt',title=identifier,version='v1',domain='consumer_reference',
                            status='published',content=identifier,content_hash='a'*64,effective_from='2026-11-14',file_path='data/source.txt',mime_type='text/plain',access_scope=scope,owner_id=owner))
                self.ticket=await DatabaseTicketStore(db).create('general','low','Bob private ticket','','bob','order-bob')
            finally:await db.close()
        asyncio.run(seed())
        fake=FakeListChatModel(responses=['{"primary_intent":"unknown","confidence":0.1}', '{"passed":true,"risk_level":"low"}'])
        workflow=create_supervisor_graph(llm=fake)
        for value in [patch.object(main,'configured_database',side_effect=self.db),patch.object(main,'graph',workflow),
                      patch.object(main,'initialize_graph'),patch.object(main,'init_tracer'),patch.object(main,'PROJECT_ROOT',self.root),
                      patch.object(main,'short_term_memory',ShortTermMemory('memory://')),
                      patch.dict(os.environ,{'RAG_ENABLED':'false','APP_MODE':'local_demo'})]:
            value.start();self.addCleanup(value.stop)
        self.client=TestClient(main.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def db(self):return Database(self.url,schema=self.schema)

    def login(self,name='alice'):
        response=self.client.post('/api/auth/login',json={'login_name':name,'password':PASSWORD})
        self.assertEqual(response.status_code,200,response.text)
        return {'Authorization':'Bearer '+response.json()['access_token']}

    def execute(self, callback):
        async def run():
            db=self.db()
            try:return await callback(db)
            finally:await db.close()
        return asyncio.run(run())

    def test_unauthenticated_direct_access_and_forged_identity_are_denied(self):
        for url in ['/api/workflow','/api/history/any','/api/runs/'+'a'*32,'/api/sessions','/api/tickets','/api/orders',
                    '/api/knowledge/documents','/api/knowledge/documents/public/file','/api/tools','/api/metrics','/api/demo/users','/api/foundation','/api/dependencies']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url,headers={'X-User-ID':'admin','X-Role':'admin'}).status_code,401)
        self.assertEqual(self.client.post('/api/chat',json={'message':'hello','user_id':'admin'}).status_code,401)
        self.assertEqual(self.client.post('/api/chat/stream',json={'message':'hello'}).status_code,401)
        self.assertEqual(self.client.post('/api/tools/call',json={'name':'ticket_create'}).status_code,401)
        self.assertEqual(self.client.get('/health/live').status_code,200)

    def test_login_password_hash_errors_and_cookie_attributes(self):
        self.assertTrue(verify_password(PASSWORD,PASSWORD_HASH));self.assertFalse(verify_password('wrong',PASSWORD_HASH))
        self.assertNotEqual(hash_password(PASSWORD),PASSWORD_HASH)
        for name in ['alice','nonexistent']:
            response=self.client.post('/api/auth/login',json={'login_name':name,'password':'wrong'})
            self.assertEqual(response.status_code,401);self.assertEqual(response.json()['error']['code'],'INVALID_CREDENTIALS')
            self.assertNotIn('wrong',response.text)
        self.login()
        cookie=self.client.cookies.get('smartcs_refresh');self.assertTrue(cookie)
        response=self.client.post('/api/auth/login',json={'login_name':'alice','password':PASSWORD,'role':'admin'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn(PASSWORD,response.text)
        response=self.client.post('/api/auth/login',json={'login_name':'alice','password':PASSWORD})
        refresh_cookie=next(x for x in response.headers.get_list('set-cookie') if x.startswith('smartcs_refresh='))
        self.assertIn('HttpOnly',refresh_cookie);self.assertIn('SameSite=strict',refresh_cookie);self.assertIn('Path=/api/auth',refresh_cookie)
        self.assertEqual(response.headers['cache-control'],'no-store')

    def test_refresh_requires_csrf_and_trusted_origin_rotates_and_rejects_replay(self):
        old=self.login();refresh=self.client.cookies.get('smartcs_refresh');csrf=self.client.cookies.get('smartcs_csrf')
        self.assertEqual(self.client.post('/api/auth/refresh').status_code,401)
        self.assertEqual(self.client.post('/api/auth/refresh',headers={'X-CSRF-Token':'wrong'}).status_code,403)
        self.assertEqual(self.client.post('/api/auth/refresh',headers={'X-CSRF-Token':csrf,'Origin':'https://evil.invalid'}).status_code,403)
        response=self.client.post('/api/auth/refresh',headers={'X-CSRF-Token':csrf,'Origin':'http://127.0.0.1:8000'})
        self.assertEqual(response.status_code,200)
        fresh={'Authorization':'Bearer '+response.json()['access_token']}
        self.assertEqual(self.client.get('/api/auth/me',headers=old).status_code,401)
        self.assertEqual(self.client.get('/api/auth/me',headers=fresh).status_code,200)
        replay=self.client.post('/api/auth/refresh',headers={'Cookie':'smartcs_refresh='+refresh,'X-CSRF-Token':csrf})
        self.assertEqual(replay.status_code,401)

    def test_logout_revokes_access_and_refresh_and_stores_only_digests(self):
        headers=self.login();raw=headers['Authorization'][7:];csrf=self.client.cookies.get('smartcs_csrf');refresh=self.client.cookies.get('smartcs_refresh')
        async def check(db):
            async with db.sessions() as session:
                row=await session.scalar(select(AuthSession).where(AuthSession.access_hash==digest(raw)))
                self.assertNotEqual(row.access_hash,raw);self.assertNotEqual(row.refresh_hash,refresh)
        self.execute(check)
        self.assertEqual(self.client.post('/api/auth/logout',headers=headers).status_code,204)
        self.assertEqual(self.client.get('/api/auth/me',headers=headers).status_code,401)
        self.assertEqual(self.client.post('/api/auth/refresh',headers={'Cookie':'smartcs_refresh='+refresh,'X-CSRF-Token':csrf}).status_code,401)

    def test_expiry_disable_and_role_changes_are_checked_on_server(self):
        headers=self.login('admin')
        async def downgrade(db):
            async with db.sessions.begin() as session:await session.execute(update(User).where(User.id=='admin').values(role='customer'))
        self.execute(downgrade)
        self.assertEqual(self.client.get('/api/metrics',headers=headers).status_code,403)
        async def disable(db):
            async with db.sessions.begin() as session:await session.execute(update(AuthCredential).where(AuthCredential.user_id=='admin').values(enabled=False))
        self.execute(disable)
        self.assertEqual(self.client.get('/api/auth/me',headers=headers).status_code,401)
        headers=self.login('alice')
        async def expire(db):
            async with db.sessions.begin() as session:await session.execute(update(AuthSession).where(AuthSession.user_id=='alice').values(access_expires_at=utcnow()-timedelta(seconds=1)))
        self.execute(expire)
        self.assertEqual(self.client.get('/api/auth/me',headers=headers).status_code,401)

    def test_persisted_login_throttle_cannot_be_bypassed_by_proxy_header(self):
        for _ in range(10):
            response=self.client.post('/api/auth/login',json={'login_name':'nobody','password':'wrong'},headers={'X-Forwarded-For':'untrusted'})
            self.assertEqual(response.status_code,401)
        response=self.client.post('/api/auth/login',json={'login_name':'nobody','password':'wrong'},headers={'X-Forwarded-For':'different'})
        self.assertEqual(response.status_code,429)
        # Separate service instance/engine still observes the persisted limit.
        async def retry(db):
            from smart_cs.schemas.errors import ApplicationError
            with self.assertRaises(ApplicationError) as caught:await AuthService(db).login('nobody','wrong','another-ip')
            self.assertEqual(caught.exception.status_code,429)
        self.execute(retry)

    def test_customer_and_support_cannot_read_other_users_orders_tickets_or_tools(self):
        bob=self.login('bob');alice=self.login('alice');support=self.login('agent')
        ticket_url='/api/tickets/'+self.ticket['ticket_id']+'?user_id=bob'
        self.assertEqual(self.client.get(ticket_url,headers=bob).status_code,200)
        for headers in [alice,support]:
            self.assertEqual(self.client.get(ticket_url,headers=headers).status_code,404)
            orders=self.client.get('/api/orders?user_id=bob',headers=headers).json()['orders']
            self.assertTrue(all(x['user_id']!='bob' for x in orders))
            result=self.client.post('/api/tools/call',headers=headers,json={'name':'order_query','arguments':{'user_id':'bob','order_id':'order-bob'}})
            self.assertEqual(result.json()['result'],[])
            for name in ['ticket_create','knowledge_search','unknown']:
                self.assertEqual(self.client.post('/api/tools/call',headers=headers,json={'name':name,'arguments':{'user_id':'bob'}}).status_code,403)
            self.assertEqual(self.client.get('/api/metrics',headers=headers).status_code,403)
        admin=self.login('admin')
        self.assertEqual(self.client.get('/api/metrics',headers=admin).status_code,200)
        self.assertEqual(self.client.get(ticket_url,headers=admin).status_code,404)

    def test_forged_body_cannot_change_chat_owner_session_history_or_run(self):
        alice=self.login('alice');bob=self.login('bob')
        response=self.client.post('/api/chat',headers=alice,json={'message':'hello','user_id':'bob'})
        self.assertEqual(response.status_code,200,response.text);reply=response.json()
        sid=reply['session_id'];run=reply['run_id']
        async def owner(db):
            async with db.sessions() as session:self.assertEqual((await session.get(ChatSession,sid)).user_id,'alice')
        self.execute(owner)
        self.assertEqual(self.client.get('/api/history/'+sid,headers=alice).status_code,200)
        self.assertEqual(self.client.get('/api/history/'+sid+'?user_id=alice',headers=bob).status_code,404)
        record=self.client.get('/api/runs/'+run+'?user_id=bob',headers=alice).json()
        self.assertEqual(record['identity']['subject_id'],'alice');self.assertTrue(record['identity']['authenticated'])
        self.assertEqual(self.client.get('/api/runs/'+run+'?user_id=alice',headers=bob).status_code,404)
        for endpoint in ['/api/chat','/api/chat/stream']:
            denied=self.client.post(endpoint,headers=bob,json={'message':'steal','session_id':sid,'user_id':'alice'})
            self.assertEqual(denied.status_code,404)
        self.assertEqual(self.client.get('/api/sessions?user_id=alice',headers=bob).json()['sessions'],[])
        # A new service/engine can verify the same persisted access token and owner.
        async def restart(db):self.assertEqual((await AuthService(db).authenticate(alice['Authorization'][7:])).subject_id,'alice')
        self.execute(restart)

    def test_restricted_catalogue_file_and_sql_context_visibility_share_policy(self):
        expected={'alice':{'public','alice-private'},'bob':{'public','bob-private'},
                  'agent':{'public','staff'},'admin':{'public','staff','management'}}
        for name,visible in expected.items():
            headers=self.login(name)
            catalogue=self.client.get('/api/knowledge/documents',headers=headers)
            self.assertEqual({d['id'] for d in catalogue.json()['documents']},visible)
            for identifier in ['public','alice-private','bob-private','staff','management']:
                file=self.client.get('/api/knowledge/documents/'+identifier+'/file',headers=headers)
                self.assertEqual(file.status_code,200 if identifier in visible else 404)
                self.assertEqual(file.headers['cache-control'],'no-store')
            async def contexts(db):
                identity=await AuthService(db).authenticate(headers['Authorization'][7:])
                token=principal_context.set(identity)
                try:
                    async with db.sessions() as session:
                        ids=set((await session.scalars(select(KnowledgeDocument.id).where(document_visibility()))).all())
                    self.assertEqual(ids,visible)
                finally:principal_context.reset(token)
            self.execute(contexts)
        async def lexical(db):self.assertEqual({d['id'] for d in await db.knowledge()},{'public'})
        self.execute(lexical)

    def test_invalid_tokens_and_token_url_or_cookie_cannot_replace_authorization(self):
        headers=self.login();token=headers['Authorization'][7:]
        for other in ['', 'fake', 'x'*300]:
            self.assertEqual(self.client.get('/api/auth/me',headers={'Authorization':'Bearer '+other}).status_code,401)
        self.assertEqual(self.client.get('/api/auth/me?access_token='+token).status_code,401)
        # Refresh cookie is present in this client, but is never sufficient for data APIs.
        self.assertEqual(self.client.get('/api/tickets').status_code,401)

    def test_concurrent_refresh_has_one_winner_and_absolute_expiry_is_enforced(self):
        self.login();refresh=self.client.cookies.get('smartcs_refresh');csrf=self.client.cookies.get('smartcs_csrf')
        async def concurrent(db):
            outcomes=await asyncio.gather(AuthService(db).refresh(refresh,csrf),AuthService(db).refresh(refresh,csrf),return_exceptions=True)
            self.assertEqual(sum(isinstance(x,tuple) for x in outcomes),1)
            self.assertEqual([x.status_code for x in outcomes if isinstance(x,Exception)],[401])
        self.execute(concurrent)
        self.login('bob');refresh=self.client.cookies.get('smartcs_refresh');csrf=self.client.cookies.get('smartcs_csrf')
        async def expire(db):
            async with db.sessions.begin() as session:await session.execute(update(AuthSession).where(AuthSession.user_id=='bob').values(refresh_expires_at=utcnow()-timedelta(seconds=1)))
        self.execute(expire)
        self.assertEqual(self.client.post('/api/auth/refresh',headers={'X-CSRF-Token':csrf}).status_code,401)

    def test_retrieval_filters_private_candidates_before_top_k(self):
        async def search(db):
            if db.engine.dialect.name!='postgresql':
                async with db.sessions() as session:
                    self.assertEqual(set((await session.scalars(select(KnowledgeDocument.id).where(document_visibility()))).all()),{'public'})
                return
            from types import SimpleNamespace
            import hashlib
            from smart_cs.db.models import RetrievalIndex,RetrievalIndexDocument,KnowledgeChunk,KnowledgeEmbedding
            from smart_cs.memory.bge_memory import BGEKnowledgeMemory
            from smart_cs.retrieval.embedding import EmbeddingAdapter
            from smart_cs.retrieval.indexes import manifest_record,digest,PROTOCOL
            async def embed(texts):return [[1.,0.]+[0.]*1022]
            models=SimpleNamespace(aembed=embed,embedding_identity=lambda:{'embedding':'test-only-fixture','dimensions':1024})
            contract=EmbeddingAdapter(models).identity()
            async with db.sessions.begin() as session:
                index=RetrievalIndex(id='test-index',active=True,model_identity=contract,configuration={})
                session.add(index);await session.flush();records=[]
                for identifier in ['bob-private','alice-private','staff','management','public']:
                    document=await session.get(KnowledgeDocument,identifier);document.source_type='business_document'
                    chunk=KnowledgeChunk(id='chunk-'+identifier,document_id=identifier,ordinal=0,content=identifier,
                        content_hash=hashlib.sha256(identifier.encode()).hexdigest())
                    session.add(chunk);await session.flush()
                    record=manifest_record(document,None,[chunk]);records.append(record)
                    session.add(RetrievalIndexDocument(index_id='test-index',**record))
                    # Restricted fixtures score higher: filters must precede LIMIT.
                    vector=([1.,0.] if identifier=='bob-private' else [1.,0.5] if identifier=='alice-private' else [1.,1.])+[0.]*1022
                    session.add(KnowledgeEmbedding(index_id='test-index',chunk_id=chunk.id,embedding=vector))
                index.configuration={'protocol':PROTOCOL,'chunker':'test','embedding_contract':contract,'chunks':5,'documents':5,
                    'manifest_sha256':digest(sorted(records,key=lambda r:r['document_id']))}
            memory=BGEKnowledgeMemory(db);memory.models=models
            for name,visible in [('alice',{'public','alice-private'}),('bob',{'public','bob-private'}),('agent',{'public','staff'})]:
                role='support' if name=='agent' else 'customer'
                token=principal_context.set(IdentityContext(subject_id=name,authenticated=True,mode='authenticated_session',role=role))
                try:
                    rows=await memory.search('test',top_k=20)
                    self.assertEqual({r['metadata']['document_id'] for r in rows},visible)
                    top=await memory.search('test',top_k=1)
                    self.assertEqual(len(top),1);self.assertIn(top[0]['metadata']['document_id'],visible)
                finally:principal_context.reset(token)
        self.execute(search)

    def test_model_supplied_user_id_cannot_create_for_bob_or_use_bobs_order(self):
        from sqlalchemy import func
        alice=self.login('alice')
        for order_id,expected_count in [('order-bob',1),('order-alice',2)]:
            fake=FakeListChatModel(responses=['{"primary_intent":"transaction","confidence":0.99,"suggested_agent":"ticket_handler"}',
                json.dumps({'action':'create','ticket_type':'general','summary':'auth verification','user_id':'bob','order_id':order_id}),
                '{"passed":true,"risk_level":"low"}'])
            graph=create_supervisor_graph(llm=fake,ticket_store=DatabaseTicketStore(main.database))
            with patch.object(main,'graph',graph):
                reply=self.client.post('/api/chat',headers=alice,json={'message':'登记申请','user_id':'bob'})
                self.assertEqual(reply.status_code,200,reply.text)
            async def verify(db):
                from smart_cs.db.models import Ticket
                async with db.sessions() as session:
                    self.assertEqual(await session.scalar(select(func.count()).select_from(Ticket)),expected_count)
                    rows=(await session.scalars(select(Ticket).where(Ticket.summary=='auth verification'))).all()
                    self.assertTrue(all(t.user_id=='alice' and t.order_id=='order-alice' for t in rows))
            self.execute(verify)

    def test_canonical_history_and_resume_api_are_owner_scoped_and_strict(self):
        alice=self.login('alice');bob=self.login('bob')
        reply=self.client.post('/api/chat',headers=alice,json={'message':'canonical history verification'})
        self.assertEqual(reply.status_code,200,reply.text)
        identifier=reply.json()['session_id']
        history=self.client.get('/api/history/'+identifier,headers=alice).json()
        self.assertEqual([m['role'] for m in history['messages']],['user','assistant'])
        self.assertIn('persistence',history);self.assertNotIn('thread_id',history)
        for payload in [{'thread_id':'forged'},{'checkpoint_id':'forged'},{'state':{}}]:
            self.assertEqual(self.client.post('/api/sessions/'+identifier+'/resume',headers=alice,json=payload).status_code,422)
        self.assertEqual(self.client.post('/api/sessions/'+identifier+'/resume',headers=bob,json={}).status_code,404)
        self.assertEqual(self.client.get('/api/sessions/'+identifier,headers=bob).status_code,404)
        self.assertEqual(self.client.get('/api/history/'+identifier,headers=alice,params={'limit':201}).status_code,422)

    def test_dense_search_uses_server_principal_and_rejects_identity_date_and_index_overrides(self):
        from unittest.mock import AsyncMock
        self.assertEqual(self.client.post('/api/knowledge/search',json={'query':'test'}).status_code,401)
        alice=self.login('alice')
        for extra in [{'user_id':'bob'},{'index_id':'forged'},{'as_of':'2030-01-01'}]:
            self.assertEqual(self.client.post('/api/knowledge/search',headers=alice,json={'query':'test',**extra}).status_code,422)
        response={'index_id':'stub-index','as_of':'2026-11-14','embedding':'test-only','documents':[],'search':'exact_cosine'}
        with patch.dict(os.environ,{'RAG_ENABLED':'true'}),patch('smart_cs.retrieval.dense.DenseRetriever.search',new_callable=AsyncMock,return_value=response) as search:
            result=self.client.post('/api/knowledge/search',headers=alice,json={'query':'test','top_k':3})
            self.assertEqual(result.status_code,200,result.text);self.assertTrue(result.json()['evidence_only'])
            self.assertEqual(search.call_args.kwargs['identity'].subject_id,'alice')


if __name__=='__main__':unittest.main()
