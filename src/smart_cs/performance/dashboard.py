"""Bounded administrator aggregates; no prompts, subjects or credentials."""
from collections import Counter,defaultdict
from sqlalchemy import select
from smart_cs.db.models import ExecutionRun,ModelCallRecord
from smart_cs.evaluation.v2_metrics import percentiles


async def snapshot(database,limit=200):
    async with database.sessions() as s:
        runs=(await s.scalars(select(ExecutionRun).order_by(ExecutionRun.created_at.desc()).limit(limit))).all()
        calls=(await s.scalars(select(ModelCallRecord).where(ModelCallRecord.run_id.in_([r.id for r in runs])))).all() if runs else []
    snapshots=[r.snapshot for r in runs];groups=defaultdict(list);nodes=defaultdict(list)
    for c in calls:groups[(c.agent_role,c.model,c.kind)].append(c.payload)
    for row in snapshots:
        for n in row.get('nodes',[]):nodes[n['node']].append(n['duration_ms'])
    perf=[r.get('performance',{}) for r in snapshots]
    return {'runs':len(runs),'limit':limit,'statuses':dict(Counter(r.status for r in runs)),
        'final_reply_ms':percentiles([r['duration_ms'] for r in snapshots if r.get('duration_ms') is not None]),
        'first_execution_progress_ms':percentiles([p['first_execution_progress_ms'] for p in perf if p.get('first_execution_progress_ms') is not None]),
        'observed_attempts':len(calls),'retries':sum(c.payload.get('attempt',1)>1 for c in calls),
        'cache_hits':sum(p.get('cache_hits',0) for p in perf),'cache_misses':sum(p.get('cache_misses',0) for p in perf),
        'known_reported_tokens':sum(c.payload.get('usage',{}).get('total_tokens') or 0 for c in calls if c.kind=='chat'),
        'unknown_usage_calls':sum(c.kind=='chat' and c.payload.get('usage',{}).get('status')!='available' for c in calls),
        'failures':dict(Counter(c.payload.get('error_code') or 'UNKNOWN' for c in calls if c.status=='failed')),
        'run_failures':dict(Counter(r.get('error_code') for r in snapshots if r.get('error_code'))),
        'nodes':[{'node':node,'latency_ms':percentiles(values)} for node,values in sorted(nodes.items())],
        'models':[{'role':role,'model':model,'kind':kind,'attempts':len(values),'failed':sum(v['status']=='failed' for v in values),
                   'known_input_tokens':sum(v.get('usage',{}).get('input_tokens') or 0 for v in values),
                   'known_output_tokens':sum(v.get('usage',{}).get('output_tokens') or 0 for v in values),
                   'unknown_usage_calls':sum(v.get('usage',{}).get('status')!='available' for v in values),
                   'latency_ms':percentiles([v['duration_ms'] for v in values]),'queue_ms':sum(v.get('queue_ms',0) for v in values)}
                  for (role,model,kind),values in sorted(groups.items())]}
