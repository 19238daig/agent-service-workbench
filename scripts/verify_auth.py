"""Run the real auth suite against a disposable PostgreSQL schema, not customer data."""
import json
import os
import re
import sys
import unittest
import asyncio
import secrets
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from psycopg import sql
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.db.database import Database
from verify_core_schema import fingerprints


def main(suite_name='test_auth',output_root=None):
    if suite_name not in {'test_auth','test_orders','test_tickets','test_conversation_persistence','test_ingestion_pipeline','test_dense_retrieval','test_action_plans','test_compliance_reviews','test_approvals','test_action_execution','test_compound_tasks','test_background_jobs','test_durable_runs','test_knowledge_admin','test_hybrid_retrieval','test_reranking','test_performance'}:raise ValueError('Unsupported verification suite')
    load_dotenv(ENV_PATH)
    url=os.environ['DATABASE_URL']
    if not url.startswith('postgresql+asyncpg://'):raise ValueError('PostgreSQL required')
    schema='foundation_verify_'+uuid4().hex[:12]
    if not re.fullmatch(r'foundation_verify_[a-f0-9]{12}',schema):raise ValueError('Unsafe verification schema')
    engine=create_engine(url.replace('+asyncpg','+psycopg',1),hide_parameters=True)
    output=PROJECT_ROOT/'.local'/({'test_orders':'orders','test_tickets':'tickets','test_conversation_persistence':'conversations','test_ingestion_pipeline':'ingestion','test_dense_retrieval':'retrieval','test_action_plans':'actions','test_compliance_reviews':'compliance','test_approvals':'approvals','test_action_execution':'execution','test_compound_tasks':'tasks','test_background_jobs':'jobs','test_durable_runs':'durable-runs','test_knowledge_admin':'product','test_hybrid_retrieval':'hybrid','test_reranking':'reranker','test_performance':'performance'}.get(suite_name,'auth'));output.mkdir(parents=True,exist_ok=True)
    if output_root is not None:
        output=Path(output_root)/suite_name;output.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'real_postgresql':True,'isolated_schema':True}
    async def public_snapshot():
        db=Database(url)
        try:return await fingerprints(db)
        finally:await db.close()
    before=asyncio.run(public_snapshot())
    role_created=False
    try:
        # The test role owns only its temporary schema. Even an erroneous
        # unqualified DROP cannot modify public business tables.
        password=secrets.token_urlsafe(32)
        with engine.begin() as connection:
            cursor=connection.connection.driver_connection.cursor()
            cursor.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(schema),sql.Literal(password)))
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{schema}"')
            connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{schema}"')
        role_created=True
        with engine.begin() as connection:
            connection.exec_driver_sql(f'SET LOCAL ROLE "{schema}"')
            if connection.scalar(text("SELECT has_table_privilege(current_user,'public.users','INSERT,UPDATE,DELETE,TRUNCATE') OR has_schema_privilege(current_user,'public','CREATE')")):
                raise RuntimeError('Verification role unexpectedly has public write privileges')
        restricted=make_url(url).set(username=schema,password=password)
        os.environ['DATABASE_URL']=restricted.render_as_string(hide_password=False)
        os.environ['AUTH_VERIFY_SCHEMA']=schema
        sys.path.insert(0,str(PROJECT_ROOT/'tests'))
        suite=unittest.defaultTestLoader.loadTestsFromName(suite_name)
        with (output/'postgresql-test-output.log').open('w',encoding='utf-8') as stream:
            result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
        if result.testsRun==0:raise RuntimeError('Verification suite was empty')
        report.update(passed=result.wasSuccessful(),tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
            failed_tests=[test.id() for test,_ in result.failures+result.errors])
    except Exception as error:report['error_type']=type(error).__name__
    finally:
        os.environ.pop('AUTH_VERIFY_SCHEMA',None)
        os.environ['DATABASE_URL']=url
        try:
            if role_created:
                with engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                    connection.exec_driver_sql(f'REVOKE USAGE ON SCHEMA public FROM "{schema}"')
                    connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{schema}"')
            report['temporary_resources_removed']=True
        except Exception as error:
            report.update(passed=False,cleanup_error_type=type(error).__name__)
        after=asyncio.run(public_snapshot())
        report['public_tables_unchanged']=before==after
        report['restricted_database_role']=True
        if before!=after:report['passed']=False
        engine.dispose()
    (output/'postgresql-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
