"""Local/CI PostgreSQL regressions with restricted, disposable roles per module."""
import argparse,json,subprocess,sys
from pathlib import Path
from smart_cs.settings import PROJECT_ROOT

MODULES=['test_auth','test_orders','test_tickets','test_conversation_persistence','test_ingestion_pipeline',
 'test_dense_retrieval','test_action_plans','test_compliance_reviews','test_approvals','test_action_execution',
 'test_compound_tasks','test_background_jobs','test_durable_runs','test_knowledge_admin','test_hybrid_retrieval','test_reranking','test_performance']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modules',nargs='+',choices=MODULES,default=MODULES)
    parser.add_argument('--child',choices=MODULES,help=argparse.SUPPRESS);args=parser.parse_args()
    root=PROJECT_ROOT/'.local/delivery/postgres';root.mkdir(parents=True,exist_ok=True)
    if args.child:
        from verify_auth import main as verify
        return verify(args.child,output_root=root)
    records={}
    for module in args.modules:
        with (root/(module+'.process.log')).open('w',encoding='utf-8') as log:
            process=subprocess.run([sys.executable,__file__,'--child',module],cwd=PROJECT_ROOT,stdout=log,stderr=log)
        path=root/module/'postgresql-verification.json'
        record=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'passed':False,'tests':0,'process_error':True}
        record['process_exit_code']=process.returncode;record['passed']=record.get('passed',False) and process.returncode==0
        records[module]=record
        print(json.dumps({'module':module,'passed':record['passed'],'tests':record.get('tests'),'skipped':record.get('skipped')}),flush=True)
    result={'passed':all(r['passed'] for r in records.values()),'tests':sum(r.get('tests',0) for r in records.values()),
      'skipped':sum(r.get('skipped',0) for r in records.values()),'modules':records,'real_postgresql':True,
      'model_provider_calls':0,'scope':'Contract fixtures; not live model quality'}
    (root/'summary.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
