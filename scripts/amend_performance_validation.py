"""One explicit validation-only amendment; no test records may exist yet."""
import asyncio
from datetime import datetime,timezone
from statistics import mean
from dotenv import load_dotenv
from evaluate_performance import OUT,bind,read,write,PROFILE,sha
from smart_cs.db.database import configured_database
from smart_cs.settings import ENV_PATH
from smart_cs.performance.rewrite import needs_rewrite

async def main():
    assert not (OUT/'comparison.json').exists() and not (OUT/'test').exists()
    assert not (OUT/'validation-amendment.json').exists()
    before=read(OUT/'validation-selection-v1.json');current=read(OUT/'selection.json');assert before==current
    db=configured_database()
    try:after=await bind(db)
    finally:await db.close()
    changed={k for k,v in before['binding']['code_sha256'].items() if after['code_sha256'][k]!=v}
    assert changed=={'src/smart_cs/performance/rewrite.py','src/smart_cs/observability/models.py','src/smart_cs/performance/dashboard.py'}
    for row in current['rewrite_validation']:
        from smart_cs.evaluation.v2_assets import rows
        case=next(r for r in rows() if r.id==row['id']);row['would_skip']=not needs_rewrite(case.message,['history'])
    positive=[r for r in current['rewrite_validation'] if r['answerable'] and r['would_skip']]
    enabled=not positive or mean(r['after']['evidence_recall'] for r in positive)>=mean(r['before']['evidence_recall'] for r in positive)-.01
    profile=current['profile'];profile['rewrite_on_demand']=enabled;write(PROFILE,profile)
    amendment={'at':datetime.now(timezone.utc).isoformat(),'changed_sources':sorted(changed),'test_read':False,
               'original_selection_sha256':sha(OUT/'validation-selection-v1.json'),
               'reasons':['Preserve existing timeout exception semantics; no model input/output change.',
                          'Conservative rewrite requires an explicit topic; ambiguous context still rewritten.',
                          'Separate run-level budget/errors from model-attempt errors in dashboard.'],
               'model_pilot_reused':'All 52 completed results retained under original development-run binding; no hidden paid replay.',
               'rewrite_recomputed':'Reuse already measured raw/rewrite evidence on validation only; recompute skip decisions.'}
    current.update(binding=after,profile=profile,profile_sha256=sha(PROFILE),rewrite_on_demand=enabled,
                   frozen_at=amendment['at'],validation_amendment=amendment)
    write(OUT/'validation-amendment.json',amendment);write(OUT/'selection.json',current)
    print({'amended_before_test':True,'rewrite_enabled':enabled,'changed_sources':len(changed)})

if __name__=='__main__':load_dotenv(ENV_PATH);asyncio.run(main())
