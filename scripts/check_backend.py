"""Local/CI backend checks without model credentials, GPU or source downloads."""
import compileall
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from uuid import uuid4
from smart_cs.evaluation.assets import sha
from smart_cs.settings import PROJECT_ROOT


def summarize(result):
    return {'passed':result.wasSuccessful(),'tests_run':result.testsRun,'failures':len(result.failures),
        'errors':len(result.errors),'skipped':len(result.skipped)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated-modules',action='store_true',help='Run all modules serially in fresh processes, retaining each result')
    parser.add_argument('--module',help=argparse.SUPPRESS);parser.add_argument('--run-key',help=argparse.SUPPRESS)
    args=parser.parse_args()
    output=PROJECT_ROOT/'.local/checks';output.mkdir(parents=True,exist_ok=True)
    if args.module:
        if not re.fullmatch(r'test_[a-z0-9_]+',args.module) or not re.fullmatch(r'[a-f0-9]{32}',args.run_key or ''):
            raise ValueError('Invalid local test module')
        if not (PROJECT_ROOT/'tests'/(args.module+'.py')).is_file():raise ValueError('Unknown local test module')
        sys.path.insert(0,str(PROJECT_ROOT/'tests'))
        result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(args.module))
        folder=output/'modules'/args.run_key;folder.mkdir(parents=True,exist_ok=True)
        (folder/(args.module+'.json')).write_text(json.dumps(summarize(result),indent=2)+'\n',encoding='utf-8')
        return 0 if result.wasSuccessful() else 1
    for name in ['synthetic-after-sales-v1','agent-eval-v1']:
        root=PROJECT_ROOT/'data'/name
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        for relative,entry in manifest['files'].items():
            path=(root/relative).resolve()
            if not path.is_relative_to(root) or sha(path.read_bytes())!=entry['sha256']:
                raise ValueError('Frozen data manifest mismatch')
    if not compileall.compile_dir(str(PROJECT_ROOT/'src'),quiet=1):return 1
    if args.isolated_modules:
        key=uuid4().hex;folder=output/'modules'/key;folder.mkdir(parents=True)
        modules={}
        for path in sorted((PROJECT_ROOT/'tests').glob('test_*.py')):
            name=path.stem
            with (folder/(name+'.log')).open('w',encoding='utf-8') as log:
                process=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--module',name,'--run-key',key],stdout=log,stderr=log,cwd=PROJECT_ROOT)
            record=folder/(name+'.json')
            modules[name]=json.loads(record.read_text(encoding='utf-8')) if record.exists() else {'passed':False,'tests_run':0,'failures':0,'errors':1,'skipped':0}
            modules[name]['process_exit_code']=process.returncode
            if process.returncode:modules[name]['passed']=False
            print(name+': '+('passed' if modules[name]['passed'] else 'FAILED'),flush=True)
        report={k:sum(v[k] for v in modules.values()) for k in ['tests_run','failures','errors','skipped']}
        report.update(passed=all(v['passed'] for v in modules.values()),execution_mode='serial_module_processes',modules=modules,module_reports=str(folder))
    else:
        suite=unittest.defaultTestLoader.discover(str(PROJECT_ROOT/'tests'))
        report=summarize(unittest.TextTestRunner(verbosity=2).run(suite))
        report['execution_mode']='single_process'
    report.update({
        'source_integration_available':(PROJECT_ROOT/'data/public-after-sales-v2/sources/seven_day_returns.html').is_file(),
        'remote_github_actions_executed':False})
    (output/'backend.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
