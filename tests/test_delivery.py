"""Clean-install recovery and delivery security boundaries; no paid model calls."""
import asyncio,hashlib,io,json,os,sys,tarfile,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import select
from smart_cs.api.auth import allowed_origins,origin_check
from smart_cs.retrieval.endpoints import validate_local_endpoint
from smart_cs.schemas.errors import ApplicationError
from smart_cs.db.database import Database
from smart_cs.db.models import AuthCredential
from smart_cs.services.auth import AuthService,verify_password
import smart_cs.delivery as delivery
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import manage_accounts


class DeliveryBoundaries(unittest.TestCase):
    def test_exact_origin_configuration(self):
        with patch.dict(os.environ,{'APP_ORIGINS':'http://127.0.0.1:18000'}):
            self.assertIn('http://127.0.0.1:18000',allowed_origins())
            origin_check(SimpleNamespace(headers={'origin':'http://127.0.0.1:18000'}))
            with self.assertRaises(ApplicationError):origin_check(SimpleNamespace(headers={'origin':'https://untrusted.invalid'}))
        for value in ['*','http://x/*','http://user:password@x','https://x?key=x','http://x/']:
            with patch.dict(os.environ,{'APP_ORIGINS':value}):
                with self.assertRaises(ValueError):allowed_origins()

    def test_container_endpoint_is_explicit_and_local(self):
        with patch('sys.platform','linux'),patch.dict(os.environ,{'SMART_CS_APP_CONTAINER':'1'}):
            self.assertEqual(validate_local_endpoint('http://host.docker.internal:61535'),'http://host.docker.internal:61535')
            for value in ['http://outside.invalid:61535','http://host.docker.internal:61535/path','http://host.docker.internal:61535?key=x']:
                with self.assertRaises(ValueError):validate_local_endpoint(value)
        with patch('sys.platform','win32'),patch.dict(os.environ,{'SMART_CS_APP_CONTAINER':'1'}):
            with self.assertRaises(ValueError):validate_local_endpoint('http://host.docker.internal:61535')

    def test_restore_rejects_path_escape_links_and_bad_hashes(self):
        for name,kind,hash_ok in [('../escape',tarfile.REGTYPE,True),('.local/auth/credentials.json',tarfile.REGTYPE,True),
                                 ('.env',tarfile.REGTYPE,True),('data/link',tarfile.SYMTYPE,True),('data/file.txt',tarfile.REGTYPE,False)]:
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);archive=root/'files.tar';payload=b'fixture';manifest=root/'manifest.json'
                with tarfile.open(archive,'w') as t:
                    member=tarfile.TarInfo(name);member.size=len(payload);member.type=kind;t.addfile(member,io.BytesIO(payload))
                manifest.write_text(json.dumps({'files':{name:{'sha256':hashlib.sha256(payload).hexdigest() if hash_ok else '0'*64}}}))
                with patch.object(delivery,'PROJECT_ROOT',root),patch.dict(os.environ,{'SMART_CS_APP_CONTAINER':'1'}):
                    with self.assertRaises(ValueError):delivery.restore_files(archive,manifest)
                self.assertFalse((root/'data/file.txt').exists())


class AccountBootstrapRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.url='sqlite+aiosqlite:///'+(self.root/'fixture.db').as_posix()
        db=Database(self.url)
        try:await db.initialize(create_schema=True)
        finally:await db.close()
        for p in [patch.object(manage_accounts,'PROJECT_ROOT',self.root),patch.object(manage_accounts,'configured_database',lambda:Database(self.url)),
                  patch.object(manage_accounts,'restrict_file',lambda p:p.chmod(0o600))]:p.start();self.addCleanup(p.stop)

    async def check_accounts(self):
        path=self.root/'.local/auth/bootstrap-accounts.json';raw=path.read_bytes();records=json.loads(raw)['accounts']
        self.assertEqual(len(records),4)
        db=Database(self.url)
        try:
            async with db.sessions() as s:
                for a in records:
                    c=await s.scalar(select(AuthCredential).where(AuthCredential.login_name==a['login_name']))
                    self.assertTrue(verify_password(a['password'],c.password_hash));self.assertEqual(c.user_id,a['user_id'])
        finally:await db.close()
        await manage_accounts.execute(SimpleNamespace(action='bootstrap'))
        self.assertEqual(path.read_bytes(),raw)

    async def test_empty_auth_directory_bootstraps_four_recoverable_accounts(self):
        await manage_accounts.execute(SimpleNamespace(action='bootstrap'));await self.check_accounts()

    async def test_interruption_after_sql_commit_retains_password_intent(self):
        original=AuthService.provision
        async def interrupted(service,*args,**kwargs):
            self.assertTrue((self.root/'.local/auth/bootstrap-pending.json').is_file())
            await original(service,*args,**kwargs)
            raise RuntimeError('Synthetic process interruption after credential commit')
        with patch.object(AuthService,'provision',interrupted):
            with self.assertRaises(RuntimeError):await manage_accounts.execute(SimpleNamespace(action='bootstrap'))
        await manage_accounts.execute(SimpleNamespace(action='bootstrap'));await self.check_accounts()


if __name__=='__main__':unittest.main()
