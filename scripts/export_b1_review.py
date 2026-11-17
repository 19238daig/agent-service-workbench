"""Export an existing B1 report for review without calling models or changing labels."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
from smart_cs.settings import PROJECT_ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',required=True);args=parser.parse_args()
    path=(PROJECT_ROOT/args.report).resolve()
    if not path.is_relative_to(PROJECT_ROOT/'.local/evaluation') or path.name!='report.json':parser.error('Select a local evaluation report.json')
    report=json.loads(path.read_text(encoding='utf-8'));folder=path.parent/'review'
    if folder.exists():parser.error('Review directory already exists; preserve any human annotations')
    folder.mkdir()
    manifest={k:report.get(k) for k in ['kind','configuration','dataset','source_manifest_sha256','policy_sha256',
        'source_code_sha256','index_id','embedding','knowledge_model','by_split','limitations']}
    manifest.update(report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),human_reviewed=False,model_calls_added=0)
    (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (folder/'latency.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['case_id','split','status','duration_ms','chat_calls','embedding_calls'])
        for row in report['results']:
            calls=row.get('calls',[]);writer.writerow([row['id'],row['split'],row['status'],row['duration_ms'],
                sum(c['kind']=='chat' for c in calls),sum(c['kind']=='embedding' for c in calls)])
    with (folder/'usage.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['case_id','kind','model','operation','status','input_tokens','output_tokens','total_tokens','usage_status','estimated_cost'])
        for row in report['results']:
            for call in row.get('calls',[]):
                usage=call.get('usage') or {}
                writer.writerow([row['id'],call['kind'],call['model'],call['operation'],call['status'],
                    *[usage.get(k) for k in ['input_tokens','output_tokens','total_tokens','status']],'unknown'])
    with (folder/'claim-review.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['case_id','split','question','claim_number','claim','quoted_sources',
            'human_entailment_supported','human_conditions_preserved','human_scope_valid','human_notes'])
        for row in report['results']:
            result=row.get('result',{})
            for claim in result.get('claims',[]):
                refs=[{'document_id':c['document_id'],'chunk_id':c['chunk_id'],'version':c['version'],
                    'page':c['page'],'heading':c['heading'],'quotes':[q['text'] for q in c['quotes'] if q['claim']==claim['number']]}
                    for c in result.get('citations',[]) if c['number'] in claim['citation_numbers']]
                writer.writerow([row['id'],row['split'],row['question'],claim['number'],claim['text'],json.dumps(refs,ensure_ascii=False),'','','',''])
    required=set(report.get('summary',{}).get('needs_review_or_failed_cases',[]))
    lines=['# 失败与待复核案例','', '这些候选标签和模型支持审查尚未独立人工复核。资料替代来源可能导致标签未命中，须查看逐例原文；不能把所有待复核案例都当作已证实的错误。','']
    for row in report['results']:
        if row['id'] not in required and row['status']!='error':continue
        result=row.get('result',{})
        lines += [f"## {row['id']} ({row['split']})",'',row['question'],'',
            f"状态：{result.get('status',row['status'])}；原因：{result.get('reason_code',row.get('error_type',''))}",'',
            result.get('answer','未完成执行。'),'',
            '候选参考要点（仅供评审，未加入模型上下文）：'+json.dumps(row['reference_points'],ensure_ascii=False),'',
            '评审记录：待人工填写。','']
    (folder/'failures.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'exported':True,'model_calls_added':0,'human_reviews_fabricated':False,'claims':report['summary']['released_claims']}))


if __name__=='__main__':main()
