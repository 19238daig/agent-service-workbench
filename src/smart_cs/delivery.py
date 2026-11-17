"""Explicit deployment initialization. No automatic migrations at API startup."""
import argparse,asyncio,os,json,hashlib,tarfile
from pathlib import Path,PurePosixPath
from dotenv import load_dotenv
from sqlalchemy import create_engine
from psycopg import sql
from smart_cs.settings import ENV_PATH,PROJECT_ROOT
from smart_cs.foundation import migrate,prepare_database
from smart_cs.db.database import configured_database
from smart_cs.persistence.postgres import prepare_checkpoint_store


def worker_grants():
    password=os.getenv('JOB_DB_PASSWORD')
    if not password:return
    role='smart_cs_jobs'
    if os.getenv('JOB_DB_USER',role)!=role:raise ValueError('Expected dedicated worker role')
    engine=create_engine(os.environ['DATABASE_URL'].replace('+asyncpg','+psycopg',1),hide_parameters=True)
    try:
        with engine.begin() as c:
            cursor=c.connection.driver_connection.cursor()
            exists=c.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname='smart_cs_jobs'").scalar()
            cursor.execute(sql.SQL(('ALTER' if exists else 'CREATE')+' ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
            for statement in [
                'GRANT USAGE ON SCHEMA public TO smart_cs_jobs',
                'GRANT EXECUTE ON FUNCTION public.smart_cs_worker_admin(text) TO smart_cs_jobs',
                'GRANT SELECT,INSERT,UPDATE ON public.background_jobs TO smart_cs_jobs',
                'GRANT SELECT,INSERT ON public.background_job_events TO smart_cs_jobs',
                'GRANT SELECT ON public.users,public.dataset_releases,public.dataset_samples TO smart_cs_jobs',
                'GRANT SELECT,INSERT ON public.knowledge_documents,public.knowledge_chunks,public.knowledge_ingestions,public.retrieval_indexes,public.retrieval_index_documents,public.knowledge_embeddings TO smart_cs_jobs',
                'GRANT UPDATE(status) ON public.knowledge_documents TO smart_cs_jobs']:
                c.exec_driver_sql(statement)
            if c.exec_driver_sql("SELECT has_table_privilege('smart_cs_jobs','public.orders','INSERT,UPDATE,DELETE') OR has_table_privilege('smart_cs_jobs','public.auth_credentials','SELECT') OR has_table_privilege('smart_cs_jobs','public.retrieval_indexes','UPDATE')").scalar():
                raise RuntimeError('Worker privileges exceed contract')
    finally:engine.dispose()


async def checkpoints():
    db=configured_database()
    try:await prepare_checkpoint_store(db)
    finally:await db.close()


def restore_files(archive,manifest):
    if os.getenv('SMART_CS_APP_CONTAINER')!='1':raise ValueError('File restore requires the target app container')
    expected=json.loads(Path(manifest).read_text(encoding='utf-8'))['files'];seen=set()
    with tarfile.open(archive,'r') as source:
        for member in source:
            name=PurePosixPath(member.name)
            prefix=name.parts[:1]==('data',) or name.parts[:2]==('.local','background-jobs')
            if not member.isfile() or name.is_absolute() or '..' in name.parts or '\\' in member.name or not prefix:
                raise ValueError('Unsafe backup member')
            if member.name not in expected or member.name in seen:raise ValueError('Unlisted/duplicate backup member')
            payload=source.extractfile(member).read()
            if hashlib.sha256(payload).hexdigest()!=expected[member.name]['sha256']:raise ValueError('Attachment checksum mismatch')
            target=(PROJECT_ROOT/member.name).resolve()
            root=(PROJECT_ROOT/('data' if name.parts[0]=='data' else '.local/background-jobs')).resolve()
            if not target.is_relative_to(root):raise ValueError('Restore path escaped data directory')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload);seen.add(member.name)
    if seen!=set(expected):raise ValueError('Missing backup attachment')
    print('Restored and checked '+str(len(seen))+' data files inside the isolated app volume.')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['init','restore-files','serve']);parser.add_argument('--seed',action='store_true')
    parser.add_argument('--archive');parser.add_argument('--manifest')
    args=parser.parse_args();load_dotenv(ENV_PATH)
    try:
        if args.action=='serve':
            import uvicorn
            uvicorn.run('smart_cs.api.main:app',host=os.getenv('HOST','0.0.0.0'),port=int(os.getenv('PORT','8000')),loop='smart_cs.persistence.loop:loop_factory')
            return 0
        if args.action=='restore-files':restore_files(args.archive,args.manifest);return 0
        migrate()
        if args.seed:asyncio.run(prepare_database())
        else:asyncio.run(checkpoints())
        worker_grants();print('Database migrations, checkpoints and configured worker grants are ready. Credentials hidden.')
        return 0
    except Exception as error:
        print('Deployment initialization failed: '+type(error).__name__);return 1


if __name__=='__main__':raise SystemExit(main())
