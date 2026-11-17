"""Human-operated review log, bound to immutable candidates; no automatic approval."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from smart_cs.settings import PROJECT_ROOT
from smart_cs.evaluation.v2_assets import rows,validate,review_status,encoded,sha


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['status','record'])
    p.add_argument('--log',default='.local/eval-v2/reviews.jsonl');p.add_argument('--case-id');p.add_argument('--decision',choices=['approved','needs_change']);p.add_argument('--reviewer');p.add_argument('--comment',default='');p.add_argument('--confirm-human-independent',action='store_true');args=p.parse_args()
    path=(PROJECT_ROOT/args.log).resolve()
    if not path.is_relative_to(PROJECT_ROOT/'.local/eval-v2'):p.error('Review log must stay in .local/eval-v2')
    if args.action=='record':
        if not args.case_id or not args.decision or not args.reviewer or not args.reviewer.strip() or not args.confirm_human_independent:p.error('Actual human reviewer and independent-review attestation are required')
        cases={r.id:r for r in rows()};case=cases.get(args.case_id)
        if case is None:p.error('Unknown case')
        dataset=validate();path.parent.mkdir(parents=True,exist_ok=True);guard=path.with_suffix('.lock');descriptor=os.open(guard,os.O_WRONLY|os.O_CREAT|os.O_EXCL)
        try:
            os.close(descriptor);review_status(path)
            prior=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
            if any(r['case_id']==case.id for r in prior):p.error('This candidate already has a decision; changed labels require a new release')
            decision={'case_id':case.id,'case_sha256':sha(encoded(case.model_dump())),'manifest_sha256':dataset['manifest_sha256'],
                'decision':args.decision,'reviewer':args.reviewer.strip(),'reviewer_kind':'human','independent_of_generation':True,
                'reviewed_at':datetime.now(timezone.utc).isoformat(),'comment':args.comment}
            with path.open('a',encoding='utf-8',newline='\n') as stream:stream.write(json.dumps(decision,ensure_ascii=False,sort_keys=True)+'\n');stream.flush();os.fsync(stream.fileno())
        finally:guard.unlink(missing_ok=True)
    print(json.dumps(review_status(path),ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
