"""Version-two fixed B1 milestones; never imports evaluation labels into serving."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.evaluation.v2_assets import ROOT,validate,rows,TICKET_MODULES,review_status,register
from smart_cs.evaluation.v2_schema import TicketV2
from smart_cs.evaluation.v2_runner import initialize,retrieve,models,read,write,check_config,lock


def contract_module(folder,module):
    sys.path.insert(0,str(PROJECT_ROOT/'tests'))
    selected=[r for r in rows() if isinstance(r,TicketV2) and r.test_module==module]
    class Results(unittest.TextTestResult):
        def addSuccess(self,test):super().addSuccess(test);records[test.id()]={'status':'passed'}
        def addFailure(self,test,error):super().addFailure(test,error);records[test.id()]={'status':'failed','error_type':error[0].__name__}
        def addError(self,test,error):super().addError(test,error);records[test.id()]={'status':'error','error_type':error[0].__name__}
        def addSkip(self,test,reason):super().addSkip(test,reason);records[test.id()]={'status':'skipped','reason':reason}
    records={};suite=unittest.TestSuite()
    for case in selected:suite.addTests(unittest.defaultTestLoader.loadTestsFromName(f'{case.test_module}.{case.test_class}.{case.test_method}'))
    with (folder/'contracts'/(module+'.log')).open('w',encoding='utf-8') as stream:result=unittest.TextTestRunner(stream=stream,verbosity=2,resultclass=Results).run(suite)
    output=[]
    for case in selected:
        key=f'{case.test_module}.{case.test_class}.{case.test_method}';record=records.get(key,{'status':'not_run','reason':'Test was not loaded'})
        output.append({'id':case.id,'task':case.task,'split':case.split,'group_id':case.group_id,'test':key,**record,'scope':'isolated executable contract; synthetic/stub models where specified','live_agent_quality':False})
    write(folder/'contracts'/(module+'.json'),{'results':output,'tests_run':result.testsRun,'passed':result.wasSuccessful()});return 0 if result.wasSuccessful() else 1


def contracts(folder):
    from collections import Counter
    validate();(folder/'contracts').mkdir(exist_ok=True)
    with lock(folder,'contracts'):
        for module in TICKET_MODULES:
            path=folder/'contracts'/(module+'.json')
            if path.exists():continue
            # One fresh process per module avoids native KDF/DB library reuse.
            with (folder/'contracts'/(module+'.process.log')).open('wb') as log:
                result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'contract-module','--run-dir',str(folder),'--module',module],cwd=PROJECT_ROOT,stdout=log,stderr=log)
            if not path.exists():write(path,{'results':[{'id':r.id,'status':'error','scope':'process failure'} for r in rows() if isinstance(r,TicketV2) and r.test_module==module],'process_exit_code':result.returncode})
            print(json.dumps({'phase':'contracts','module':module,'exit_code':result.returncode}),flush=True)
        results=[r for module in TICKET_MODULES for r in read(folder/'contracts'/(module+'.json'))['results']];counts=dict(Counter(r['status'] for r in results))
        report={'selected':100,'completed':len(results)==100,'counts':counts,'results':results,'real_business_rows_modified':False,'real_agent_success_rate':None}
        write(folder/'contract-summary.json',report);return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['init','retrieval','models','contracts','contract-module','validate','review-status','register'])
    p.add_argument('--run-dir');p.add_argument('--concurrency',type=int,default=4);p.add_argument('--module',choices=TICKET_MODULES);p.add_argument('--review-log',default='.local/eval-v2/reviews.jsonl');args=p.parse_args();load_dotenv(ENV_PATH)
    if args.action=='validate':value=validate()
    elif args.action=='review-status':value=review_status(PROJECT_ROOT/args.review_log)
    elif args.action=='register':
        from smart_cs.db.database import configured_database
        async def registration():
            db=configured_database()
            try:return await register(db)
            finally:await db.close()
        value=asyncio.run(registration())
    else:
        if not args.run_dir:p.error('--run-dir required')
        folder=(PROJECT_ROOT/args.run_dir).resolve()
        if not folder.is_relative_to(PROJECT_ROOT/'.local/evaluation'):p.error('Run directory must stay under .local/evaluation')
        if args.action=='contract-module':
            if not args.module:p.error('--module required')
            return contract_module(folder,args.module)
        if args.action=='init':value=asyncio.run(initialize(folder))
        elif args.action=='retrieval':value=asyncio.run(retrieve(folder))
        elif args.action=='models':value=asyncio.run(models(folder,args.concurrency))
        else:
            from smart_cs.db.database import configured_database
            async def check():
                db=configured_database()
                try:await check_config(folder,db)
                finally:await db.close()
            asyncio.run(check());value=contracts(folder)
    print(json.dumps(value,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
