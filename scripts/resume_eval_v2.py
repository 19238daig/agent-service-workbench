"""Explicit date-pinned recovery, retaining the interrupted experiment unchanged."""
import argparse
import asyncio
from datetime import datetime,timezone,timedelta
import difflib
import json
from pathlib import Path
import shutil
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.evaluation.v2_assets import validate,sha,rows
from smart_cs.evaluation.v2_runner import initialize,code_fingerprints,read,write


def folder(value):
    p=(PROJECT_ROOT/value).resolve()
    if not p.is_relative_to(PROJECT_ROOT/'.local/evaluation'):raise ValueError('Run paths must remain beneath .local/evaluation')
    return p


async def recover(source,target,proof,retry_interrupted=False):
    original=read(source/'run.json');dataset=validate()
    if original['dataset']!=dataset:raise ValueError('Original dataset differs')
    fingerprints=code_fingerprints();changed={name for name in fingerprints if original['code_sha256'].get(name)!=fingerprints[name]}
    runner='src/smart_cs/evaluation/v2_runner.py'
    if changed!={runner} or sha(proof.read_bytes())!=original['code_sha256'][runner]:raise ValueError('Only the documented offline clock/resume repair can adopt old results')
    stored=[];interrupted=[];zone=timezone(timedelta(hours=8))
    expected={r.id for r in rows() if r.split=='test' and r.task!='ticket_workflow'}
    for p in sorted((source/'models').glob('*.json')):
        record=read(p)
        if record['id'] not in expected:raise ValueError('Unknown original case')
        if record['status']=='started':interrupted.append(record['id']);continue
        if datetime.fromtimestamp(p.stat().st_mtime,zone).date().isoformat()!=original['as_of']:raise ValueError('Cannot adopt an answer written on a different business date')
        stored.append((p,record))
    if interrupted and not retry_interrupted:raise ValueError('Explicit --retry-interrupted required; prior calls/usage may be unknown')
    config=await initialize(target,as_of=original['as_of'])
    for key in ['index_id','index_configuration_sha256','embedding','roles','policy_sha256','configuration','versions']:
        if config[key]!=original[key]:raise ValueError('Runtime comparison condition changed: '+key)
    diff=''.join(difflib.unified_diff(proof.read_text(encoding='utf-8').splitlines(True),(PROJECT_ROOT/runner).read_text(encoding='utf-8').splitlines(True),fromfile='original runner',tofile='frozen-clock runner'))
    (target/'recovery-code.diff').write_text(diff,encoding='utf-8')
    adoption=[]
    for p,record in stored:
        digest=sha(p.read_bytes());adopted={**record,'adopted_from':p.relative_to(PROJECT_ROOT).as_posix(),'original_result_sha256':digest}
        write(target/'models'/p.name,adopted);adoption.append({'id':record['id'],'source_sha256':digest,'source':p.relative_to(PROJECT_ROOT).as_posix()})
    for name in ['retrieval','contracts']:
        shutil.copytree(source/name,target/name)
    for name in ['retrieval-summary.json','contract-summary.json']:shutil.copyfile(source/name,target/name)
    report={'source_run':source.relative_to(PROJECT_ROOT).as_posix(),'target_run':target.relative_to(PROJECT_ROOT).as_posix(),
        'original_run_unchanged':True,'adopted_completed_results':len(stored),'adopted':adoption,'interrupted_cases_explicitly_retried':interrupted,
        'remaining_not_previously_started':len(expected)-len(stored)-len(interrupted),'frozen_business_date':original['as_of'],
        'clock_mode':'frozen_offline','recovery_code_diff_sha256':sha(diff.encode()),
        'prior_interrupted_provider_call_count':'unknown','prior_interrupted_usage':'unknown',
        'same_model_aliases':True,'underlying_provider_model_revision_unverified':True,
        'no_prompt_threshold_or_label_tuning':True,'source_proof_sha256':sha(proof.read_bytes())}
    write(target/'recovery.json',report);return {k:v for k,v in report.items() if k!='adopted'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--target',required=True);p.add_argument('--retry-interrupted',action='store_true');args=p.parse_args();load_dotenv(ENV_PATH)
    value=asyncio.run(recover(folder(args.source),folder(args.target),PROJECT_ROOT/'.local/eval-v2/runner-before-date-pin.py',args.retry_interrupted));print(json.dumps(value,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
