"""Re-run all 100 mapped contracts using fresh restricted PostgreSQL roles."""
import asyncio,json,os,secrets,subprocess,sys
from uuid import uuid4
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from psycopg import sql
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.evaluation.v2_assets import rows,validate
from smart_cs.evaluation.v2_schema import TicketV2
from evaluate_quality import OUT,write,read


def child(module):
    from evaluate_v2 import contract_module
    return contract_module(OUT,module)


def main():
    validate();folder=OUT/'contracts';folder.mkdir(exist_ok=True)
    selected=[r for r in rows() if isinstance(r,TicketV2)]
    root=os.environ['DATABASE_URL'];engine=create_engine(root.replace('+asyncpg','+psycopg',1),hide_parameters=True)
    try:
        for module in sorted({r.test_module for r in selected}):
            if (folder/(module+'.json')).exists():continue
            schema='foundation_verify_'+uuid4().hex[:12];password=secrets.token_urlsafe(32)
            try:
                with engine.begin() as c:
                    cursor=c.connection.driver_connection.cursor()
                    cursor.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(schema),sql.Literal(password)))
                    cursor.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(sql.Identifier(schema),sql.Identifier(schema)))
                    cursor.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(schema)))
                    cursor.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(schema)))
                    assert not c.scalar(text("SELECT has_table_privilege(current_user,'public.orders','INSERT,UPDATE,DELETE,TRUNCATE') OR has_schema_privilege(current_user,'public','CREATE')"))
                env=os.environ.copy();env['DATABASE_URL']=make_url(root).set(username=schema,password=password).render_as_string(hide_password=False);env['AUTH_VERIFY_SCHEMA']=schema
                with (folder/(module+'.process.log')).open('wb') as log:
                    result=subprocess.run([sys.executable,__file__,'--child',module],env=env,cwd=PROJECT_ROOT,stdout=log,stderr=log)
                if not (folder/(module+'.json')).exists():
                    write(folder/(module+'.json'),{'results':[{'id':r.id,'status':'error','scope':'isolated process failure'} for r in selected if r.test_module==module]})
                print(json.dumps({'module':module,'exit_code':result.returncode}),flush=True)
            finally:
                with engine.begin() as c:
                    cursor=c.connection.driver_connection.cursor()
                    cursor.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
                    cursor.execute(sql.SQL('REVOKE USAGE ON SCHEMA public FROM {}').format(sql.Identifier(schema)))
                    cursor.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(schema)))
        results=[r for module in sorted({r.test_module for r in selected}) for r in read(folder/(module+'.json'))['results']]
        report={'selected':len(selected),'results':results,'counts':dict(Counter(r['status'] for r in results)),
                'real_postgresql':True,'restricted_roles':True,'temporary_resources_removed':True,'synthetic_models_not_live_agent_quality':True}
        write(OUT/'contract-summary.json',report);print(json.dumps({k:v for k,v in report.items() if k!='results'}))
    finally:engine.dispose()


if __name__=='__main__':
    load_dotenv(ENV_PATH)
    if len(sys.argv)==3 and sys.argv[1]=='--child':raise SystemExit(child(sys.argv[2]))
    main()
