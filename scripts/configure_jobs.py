"""Provision only the dedicated worker's DB grants. Never print credentials."""
import os
import secrets
from dotenv import load_dotenv,set_key
from sqlalchemy import create_engine
from psycopg import sql
from smart_cs.settings import ENV_PATH,PROJECT_ROOT

def main():
    load_dotenv(ENV_PATH)
    role='smart_cs_jobs'
    if os.getenv('JOB_DB_USER',role)!=role:raise RuntimeError('Expected dedicated worker role')
    password=os.getenv('JOB_DB_PASSWORD') or secrets.token_urlsafe(32)
    engine=create_engine(os.environ['DATABASE_URL'].replace('+asyncpg','+psycopg',1),hide_parameters=True)
    try:
        with engine.begin() as c:
            cursor=c.connection.driver_connection.cursor()
            if not c.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname='smart_cs_jobs'").scalar():
                cursor.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
            else:cursor.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
            c.exec_driver_sql('GRANT USAGE ON SCHEMA public TO smart_cs_jobs')
            c.exec_driver_sql('GRANT EXECUTE ON FUNCTION public.smart_cs_worker_admin(text) TO smart_cs_jobs')
            c.exec_driver_sql('GRANT SELECT,INSERT,UPDATE ON public.background_jobs TO smart_cs_jobs')
            c.exec_driver_sql('GRANT SELECT,INSERT ON public.background_job_events TO smart_cs_jobs')
            c.exec_driver_sql('GRANT SELECT ON public.users,public.dataset_releases,public.dataset_samples TO smart_cs_jobs')
            c.exec_driver_sql('GRANT SELECT,INSERT ON public.knowledge_documents,public.knowledge_chunks,public.knowledge_ingestions,public.retrieval_indexes,public.retrieval_index_documents,public.knowledge_embeddings TO smart_cs_jobs')
            c.exec_driver_sql('GRANT UPDATE(status) ON public.knowledge_documents TO smart_cs_jobs')
            if c.exec_driver_sql("SELECT has_table_privilege('smart_cs_jobs','public.orders','INSERT,UPDATE,DELETE') OR has_table_privilege('smart_cs_jobs','public.auth_credentials','SELECT') OR has_table_privilege('smart_cs_jobs','public.retrieval_indexes','UPDATE')").scalar():raise RuntimeError('Worker has excessive privileges')
        set_key(str(ENV_PATH),'JOB_DB_USER',role)
        set_key(str(ENV_PATH),'JOB_DB_PASSWORD',password)
        set_key(str(ENV_PATH),'JOB_BROKER_URL','redis://127.0.0.1:56380/0')
        set_key(str(ENV_PATH),'JOB_WORKER_AUTO_START','true')
        (PROJECT_ROOT/'.local/background-jobs').mkdir(parents=True,exist_ok=True)
        print('Dedicated worker configured; credential hashes/business writes/index activation denied.')
    finally:engine.dispose()

if __name__=='__main__':main()
