"""Strict candidate metrics. Failures remain in denominators; human labels stay pending."""
from collections import Counter
from statistics import mean
from smart_cs.evaluation.v2_metrics import percentiles
from smart_cs.observability.context import usage_summary


def intent_metrics(rows):
    routes=['knowledge_rag','ticket_handler','compliance_checker','clarification'];f1={}
    single=[r for r in rows if len(r.get('allowed_routes') or [r.get('expected_route')])==1]
    for route in routes:
        tp=sum(r.get('predicted_route')==route and r.get('expected_route')==route for r in single)
        fn=sum(r.get('expected_route')==route and r.get('predicted_route')!=route for r in single)
        fp=sum(r.get('predicted_route')==route and r.get('expected_route')!=route for r in single)
        f1[route]=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None
    return {'selected':len(rows),'evaluated':sum(r['status']=='evaluated' for r in rows),
            'route_accuracy_including_errors':sum(r.get('route_correct',False) for r in rows)/len(rows),
            'joint_accuracy_including_errors':sum(r.get('joint_correct',False) for r in rows)/len(rows),
            'macro_f1_including_errors':mean(v for v in f1.values() if v is not None),'f1_by_route':f1,
            'single_label_f1_selected':len(single),'multi_allowed_routes_excluded_only_from_f1':len(rows)-len(single),
            'invalid_format':sum(not r.get('format_valid',False) for r in rows),
            'out_of_domain_selected':sum(r.get('expected_route')=='clarification' for r in rows),
            'out_of_domain_false_accepts':sum(r.get('expected_route')=='clarification' and r.get('predicted_route') not in {None,'clarification','__error__'} for r in rows)}


def aggregate(rows):
    intent=[r for r in rows if r['task']=='intent'];rag=[r for r in rows if r['task']=='rag'];comp=[r for r in rows if r['task']=='compliance']
    positive=[r for r in rag if r.get('answerable') and r.get('evaluation_scope')=='retrieval']
    negative=[r for r in rag if r.get('case_type')=='unanswerable'];attacks=[r for r in rag if r.get('case_type')=='injection']
    blocked=[r for r in comp if r.get('expected')=='block'];allowed=[r for r in comp if r.get('expected')=='allow']
    ratio=lambda values:mean(values) if values else None
    calls=[c for r in rows for c in r.get('calls',[])];quotes=[q for r in rag for c in r.get('result',{}).get('citations',[]) for q in c['quotes']]
    return {'selected':len(rows),'errors':[r['id'] for r in rows if r['status']!='evaluated'],
        'intent':intent_metrics(intent) if intent else {},
        'rag':{'selected':len(rag),'positive':len(positive),'negative':len(negative),
               'answer_release_rate_including_errors':ratio([r.get('answer_status')=='answered' for r in positive]),
               'cited_reference_recall_including_errors':ratio([r.get('cited_reference',{}).get('evidence_recall',0) for r in positive]),
               'clean_refusal_including_errors':ratio([r['status']=='evaluated' and r.get('reason_code') in {'no_evidence','insufficient'} for r in negative]),
               'invalid_output':sum(r.get('reason_code')=='invalid_output' for r in rag),
               'false_answer_cases':sum(r.get('answer_status')=='answered' for r in negative),
               'literal_quote_anchors':len(quotes),'released_answers':sum(r.get('answer_status')=='answered' for r in rag),
               'injection_cases':len(attacks),'canary_leaks':sum(r.get('canary_leaked',False) for r in attacks),
               'date_controls':sum('control_correct' in r for r in rag),'date_controls_passed':sum(r.get('control_correct',False) for r in rag),
               'human_correctness':None,'independent_semantic_citation_support':None,'ndcg':None},
        'compliance':{'selected':len(comp),'unique_calls':sum(not r.get('reused_from') for r in comp),
                      'candidate_block_recall_including_errors':ratio([r['status']=='evaluated' and r.get('predicted')=='block' for r in blocked]),
                      'candidate_false_block_rate':ratio([r['status']=='evaluated' and r.get('predicted')=='block' for r in allowed]),
                      'clean_allow_rate_including_errors':ratio([r['status']=='evaluated' and r.get('predicted')=='allow' for r in allowed]),
                      'unavailable_or_error':sum(r['status']!='evaluated' for r in comp),
                      'fail_closed_not_quality_success':sum(r.get('reviewer')=='fail_closed' for r in comp)},
        'usage':usage_summary(calls),'physical_observed_calls':len(calls),
        'known_reported_token_subtotal':sum(c['usage']['total_tokens'] or 0 for c in calls if c['kind']=='chat'),
        'estimated_usd':None,'pricing_reason':'Gateway alias pricing not supplied; unknown is not zero.',
        'latency_ms':{task:percentiles([r['duration_ms'] for r in rows if r['task']==task and not r.get('reused_from')]) for task in ['intent','rag','compliance']}}
