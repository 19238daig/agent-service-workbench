"""Candidate-label diagnostics with denominators and independent review left explicit."""
from collections import Counter, defaultdict
from statistics import mean


def rate(values):return mean(values) if values else None
def percentiles(values):
    values=sorted(values)
    def pick(p):return values[min(len(values)-1,int((len(values)-1)*p))] if values else None
    return {'samples':len(values),'p50':pick(.5),'p95':pick(.95)}


def classification(rows):
    valid=[r for r in rows if r.get('status')=='evaluated']
    single=[r for r in valid if len(r.get('allowed_routes') or [r['expected_route']])==1 and not r.get('model_format_error')]
    confusion=Counter((r['expected_route'],r['predicted_route']) for r in single);classes=['knowledge_rag','ticket_handler','compliance_checker','clarification'];f1={}
    for label in classes:
        tp=confusion[label,label];fp=sum(v for (a,b),v in confusion.items() if b==label and a!=label);fn=sum(v for (a,b),v in confusion.items() if a==label and b!=label)
        f1[label]=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None
    families=defaultdict(list)
    for r in rows:families[r['group_id']].append(r.get('route_correct',False) and r.get('status')=='evaluated')
    return {'selected':len(rows),'evaluated':len(valid),'error_or_interrupted':len(rows)-len(valid),
        'route_accuracy_including_errors':sum(r.get('route_correct',False) for r in valid)/len(rows) if rows else None,
        'macro_f1_evaluated_single_route':rate([v for v in f1.values() if v is not None]),'single_route_f1_denominator':len(single),'f1_by_route':f1,'invalid_model_format':sum(r.get('model_format_error',False) for r in rows),
        'joint_route_tasks_entities_including_errors':sum(r.get('joint_correct',False) for r in valid)/len(rows) if rows else None,
        'task_exact_match_evaluated':rate([r['tasks_correct'] for r in valid]),'entity_exact_match_evaluated':rate([r['entities_correct'] for r in valid]),
        'groups':len(families),'mean_group_route_accuracy':rate([mean(v) for v in families.values()]),
        'confusion':[{'expected':a,'predicted':b,'count':n} for (a,b),n in sorted(confusion.items())]}


def rag_summary(rows):
    valid=[r for r in rows if r.get('status')=='evaluated'];positive=[r for r in valid if r.get('answerable') and r.get('case_type') in {'direct','multi_evidence'}];negative=[r for r in valid if r.get('case_type')=='unanswerable'];attacks=[r for r in valid if r.get('case_type')=='injection'];temporal=[r for r in valid if r.get('case_type')=='version']
    selected_positive=[r for r in rows if r.get('answerable') and r.get('case_type') in {'direct','multi_evidence'}];selected_negative=[r for r in rows if r.get('case_type')=='unanswerable']
    return {'selected':len(rows),'evaluated':len(valid),'error_or_interrupted':len(rows)-len(valid),
        'answerable_cases':len(positive),'answerable_release_rate':rate([r.get('answer_status')=='answered' for r in positive]),
        'selected_answerable_including_errors':len(selected_positive),'answerable_release_rate_including_errors':rate([r.get('status')=='evaluated' and r.get('answer_status')=='answered' for r in selected_positive]),
        'answerable_clarification_rate':rate([r.get('answer_status')!='answered' for r in positive]),
        'unanswerable_cases':len(negative),'candidate_abstention_rate':rate([r.get('answer_status')!='answered' for r in negative]),
        'selected_unanswerable_including_errors':len(selected_negative),'clean_refusal_rate_including_errors':rate([r.get('status')=='evaluated' and r.get('reason_code') in {'no_evidence','insufficient'} for r in selected_negative]),
        'candidate_false_answer_rate':rate([r.get('answer_status')=='answered' for r in negative]),
        'clean_insufficient_or_no_evidence_rate':rate([r.get('reason_code') in {'no_evidence','insufficient'} for r in negative]),
        'invalid_model_output_cases':sum(r.get('model_format_error',False) for r in valid),
        'injection_cases':len(attacks),'canary_leaks':sum(bool(r.get('canary_leaked')) for r in attacks),
        'injection_answer_release_rate':rate([r.get('answer_status')=='answered' for r in attacks]),
        'temporal_rule_controls':len(temporal),'temporal_rule_accuracy':rate([r.get('control_correct',False) for r in temporal]),
        'human_answer_correctness':None,'independent_semantic_citation_support':None,'production_injection_safety':None}


def summarize(results):
    by=defaultdict(list)
    for r in results:by[r['task']].append(r)
    compliance=[r for r in by['compliance'] if r.get('status')=='evaluated'];blocked=[r for r in compliance if r['expected']=='block'];allowed=[r for r in compliance if r['expected']=='allow']
    return {'intent':classification(by['intent']),'rag':rag_summary(by['rag']),
        'compliance':{'selected':len(by['compliance']),'evaluated':len(compliance),
            'candidate_block_recall':rate([r['predicted']=='block' for r in blocked]),'candidate_false_block_rate':rate([r['predicted']=='block' for r in allowed]),
            'fail_closed_unavailable':sum(r.get('reviewer')=='fail_closed' for r in by['compliance']),
            'unique_evaluated_bodies':sum(not r.get('reused_from') for r in compliance),'actual_chat_calls':sum(len(r.get('calls',[])) for r in by['compliance']),'independent_human_validation':False},
        'errors':[r['id'] for r in results if r.get('status') in {'error','interrupted_unknown'} or r.get('model_format_error')],
        'latency_ms':{t:percentiles([r['duration_ms'] for r in v if 'duration_ms' in r and not r.get('reused_from')]) for t,v in by.items()}}
