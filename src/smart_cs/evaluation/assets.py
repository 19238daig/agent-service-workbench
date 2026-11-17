import hashlib
import json
import random
import re
from collections import Counter,defaultdict
from pathlib import Path
from smart_cs.settings import PROJECT_ROOT
from smart_cs.ingestion.parsers import parse
from smart_cs.evaluation.schema import CASE_ADAPTER,RagCase
from smart_cs.evaluation.cases import candidates

RELEASE='agent-eval-v1'
ROOT=PROJECT_ROOT/'data'/RELEASE
CORPUS=PROJECT_ROOT/'data/public-after-sales-v2'

def sha(data):return hashlib.sha256(data).hexdigest()
def encoded(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')
def normalize(value):return re.sub(r'\s+','',value).casefold()
def records(root=ROOT):
    return [CASE_ADAPTER.validate_json(line) for p in sorted(Path(root).glob('*/*.jsonl')) if p.parent.name in {'rag','intent','ticket_workflow','compliance'} for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]


def corpus_material(corpus=CORPUS):
    corpus=Path(corpus)
    manifest=json.loads((corpus/'source_manifest.json').read_text(encoding='utf-8'))
    sources={s['file']:s for s in manifest['sources']}
    blocks={}
    for name,source in sources.items():
        path=(corpus/'sources'/name).resolve()
        if not path.is_relative_to((corpus/'sources').resolve()):raise ValueError('Unsafe source path')
        if sha(path.read_bytes())!=source['sha256']:raise ValueError('Source checksum changed')
        blocks[name]=parse(path)
    return manifest,sources,blocks


def _reference_factory(sources,blocks):
    def locate(name,quote,page=None):
        found=next((b for b in blocks[name] if normalize(quote) in normalize(b.text) and (page is None or b.page==page)),None)
        if found is None:raise ValueError(f'Evidence not found: {name}: {quote}')
        source=sources[name]
        return {'source':name,'document_id':'file-'+source['sha256'][:32],
            'source_sha256':source['sha256'],'version':source['version'],'source_url':source['url'],
            'page':found.page,'heading':found.heading,'quote':quote,'paragraph_sha256':sha(found.text.encode()),
            'supporting_text':found.text}
    def evidence(name,quote,page=None):
        alternatives=[locate(name,quote,page)]
        if name in {'consumer_protection_regulation.html','consumer_protection_regulation.pdf'}:
            other='consumer_protection_regulation.pdf' if name.endswith('.html') else 'consumer_protection_regulation.html'
            try:alternatives.append(locate(other,quote))
            except ValueError:pass
        return {'alternatives':alternatives}
    return evidence


def leakage_keys(case):
    if isinstance(case,RagCase):
        keys=set()
        for group in case.evidence:
            for ref in group.alternatives:
                source='consumer_protection_regulation' if ref.source.startswith('consumer_protection_regulation.') else ref.source
                article=re.search(r'第[一二三四五六七八九十百零\d]+条',ref.heading)
                position=article.group() if article else ref.heading or f'page-{ref.page}'
                keys.add(source+':'+position)
        return keys or {case.group_id}
    return {case.group_id}


def assign_splits(rows):
    # Shared evidence sections and PDF/HTML duplicates form connected groups.
    parent={r.id:r.id for r in rows}
    def find(x):
        while parent[x]!=x:parent[x]=parent[parent[x]];x=parent[x]
        return x
    seen={}
    for row in rows:
        for key in leakage_keys(row):
            if key in seen:parent[find(row.id)]=find(seen[key])
            else:seen[key]=row.id
    groups=defaultdict(list)
    for row in rows:groups[(row.task,find(row.id))].append(row)
    for task in {r.task for r in rows}:
        buckets=[v for (t,_),v in groups.items() if t==task]
        random.Random('step03:'+task).shuffle(buckets);buckets.sort(key=len,reverse=True)
        total=sum(map(len,buckets));remaining={'train':round(total*.7),'validation':max(1,round(total*.1))}
        remaining['test']=total-sum(remaining.values())
        for group in buckets:
            fits=[s for s,n in remaining.items() if n>=len(group)]
            split=max(fits or list(remaining),key=lambda s:remaining[s])
            remaining[split]-=len(group)
            group_id=task+'-g-'+sha('|'.join(sorted(r.id for r in group)).encode())[:12]
            for row in group:row.group_id=group_id;row.split=split
    return rows


def build(root=ROOT,corpus=CORPUS):
    root=Path(root);corpus=Path(corpus)
    if (root/'manifest.json').exists():return validate(root,corpus)
    source_manifest,sources,blocks=corpus_material(corpus)
    rows=assign_splits(candidates(_reference_factory(sources,blocks)))
    files={}
    for task in sorted({r.task for r in rows}):
        for split in ['train','validation','test']:
            selected=[r.model_dump() for r in rows if r.task==task and r.split==split]
            if not selected:continue
            relative=f'{task}/{split}.jsonl'
            data=''.join(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n' for row in selected).encode()
            files[relative]={'sha256':sha(data),'records':len(selected)}
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    schema=encoded(CASE_ADAPTER.json_schema());(root/'schema.json').write_bytes(schema)
    files['schema.json']={'sha256':sha(schema),'records':0}
    manifest={'id':RELEASE,'version':'v1','created_on':'2026-11-13','human_reviewed':False,
        'gold_status':'candidate_pending_independent_human_review','provenance':'ai_authored_cases_grounded_in_public_sources_and_synthetic_contracts',
        'corpus':source_manifest['id'],'source_manifest_sha256':sha((corpus/'source_manifest.json').read_bytes()),
        'contract_sha256':{'docs/business-scope.md':sha((PROJECT_ROOT/'docs/business-scope.md').read_bytes())},
        'split_policy':'connected evidence-section groups; deterministic greedy approximately 70/10/20 by task; train means development only',
        'seed':'step03:<task>','files':files,'rag_candidates':50,'total_cases':len(rows),
        'forbidden_uses':['Do not copy evaluation questions, reference answers or labels into the knowledge corpus or prompt examples from validation/test.',
                          'Do not claim independently human-reviewed gold labels or real-world production accuracy.']}
    (root/'manifest.json').write_bytes(encoded(manifest))
    export_review(root)
    return validate(root,corpus)


def validate(root=ROOT,corpus=CORPUS):
    root=Path(root);corpus=Path(corpus)
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    for relative,entry in manifest['files'].items():
        path=(root/relative).resolve()
        if not path.is_relative_to(root.resolve()) or sha(path.read_bytes())!=entry['sha256']:raise ValueError('Dataset asset changed; publish a new release')
    if sha((corpus/'source_manifest.json').read_bytes())!=manifest['source_manifest_sha256']:raise ValueError('Corpus manifest drift')
    for relative,digest in manifest['contract_sha256'].items():
        if sha((PROJECT_ROOT/relative).read_bytes())!=digest:raise ValueError('Business contract drift')
    _,sources,blocks=corpus_material(corpus)
    rows=records(root);ids=set();group_split={};key_split={};texts={}
    for row in rows:
        if row.id in ids:raise ValueError('Duplicate case ID')
        ids.add(row.id)
        if group_split.setdefault(row.group_id,row.split)!=row.split:raise ValueError('Group leaked across splits')
        for key in leakage_keys(row):
            if key_split.setdefault(key,row.split)!=row.split:raise ValueError('Evidence family leaked across splits')
        text=normalize(row.message+getattr(row,'candidate_answer',''))
        if text in texts:raise ValueError('Duplicate input text')
        texts[text]=row.id
        if isinstance(row,RagCase):
            for group in row.evidence:
                for ref in group.alternatives:
                    source=sources.get(ref.source)
                    if source is None or source['sha256']!=ref.source_sha256:raise ValueError('Evidence source drift')
                    if not any(sha(b.text.encode())==ref.paragraph_sha256 and normalize(ref.quote) in normalize(b.text) and b.page==ref.page and b.heading==ref.heading for b in blocks[ref.source]):
                        raise ValueError('Evidence quote or locator missing')
    expected_paths={f'{r.task}/{r.split}.jsonl' for r in rows}
    actual_paths={str(p.relative_to(root)).replace('\\','/') for p in root.glob('*/*.jsonl') if p.parent.name in {'rag','intent','ticket_workflow','compliance'}}
    if expected_paths!=actual_paths or len(rows)!=manifest['total_cases']:raise ValueError('Dataset file set/count mismatch')
    for relative,entry in manifest['files'].items():
        if relative.endswith('.jsonl') and sum(r.task+'/'+r.split+'.jsonl'==relative for r in rows)!=entry['records']:raise ValueError('Manifest record count mismatch')
    rag=[r for r in rows if r.task=='rag']
    if len(rag)!=50:raise ValueError('Exactly 50 RAG review candidates required')
    return {'passed':True,'release_id':manifest['id'],'manifest_sha256':sha((root/'manifest.json').read_bytes()),
        'total_cases':len(rows),'rag_candidates':len(rag),'human_gold_cases':0,
        'counts':dict(sorted(Counter(r.task+'/'+r.split for r in rows).items())),
        'rag_types':dict(Counter(r.case_type for r in rag)),'groups':len(group_split),'corpus_sources':len(sources)}


def export_review(root=ROOT):
    root=Path(root);rows=records(root)
    lines=['# 第三步：RAG 候选逐条人工复核清单','','这 50 条由 AI 编写并程序核对原文定位，尚未成为人工金标准。请核对问题、依据、参考要点、可回答性与版本范围。修改标签需发布新数据版本；复核决定引用当前 case_sha256。','']
    for row in rows:
        if not isinstance(row,RagCase):continue
        digest=sha(encoded(row.model_dump()))
        lines += [f'## {row.id} · {row.split}',f'- 分组：`{row.group_id}`；类型：`{row.case_type}`；可回答：`{row.answerable}`',f'- 问题：{row.message}',f'- 参考要点：{"；".join(row.reference_points)}',f'- case_sha256：`{digest}`']
        for i,group in enumerate(row.evidence,1):
            for ref in group.alternatives:
                fragment=f'#page={ref.page}' if ref.page else ''
                lines += [f'- 证据组{i}：[原文件 {ref.source}](../../public-after-sales-v2/sources/{ref.source}{fragment})；页/章节：{ref.page or ref.heading}；版本：{ref.version}',f'  - 原文定位短语：{ref.quote}',f'  - 原文段落：{ref.supporting_text}']
        if row.untrusted_context:lines += [f'- 只供测试的非可信上下文：`{row.untrusted_context}`']
        lines += ['- [ ] 依据与要点正确；[ ] 可回答性正确；[ ] 版本及范围明确；[ ] 独立复核完成','']
    folder=root/'review';folder.mkdir(exist_ok=True)
    (folder/'rag-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    first=next(r for r in rows if isinstance(r,RagCase))
    example={'case_id':first.id,'case_sha256':sha(encoded(first.model_dump())),'decision':'approved','reviewer':'请填写实际复核人','reviewed_at':'请填写带时区的ISO时间','comment':'请填写实际复核意见；这只是格式示例'}
    (folder/'decision-format.json').write_bytes(encoded(example))


def review_status(path,root=ROOT):
    from datetime import datetime
    rows={r.id:r for r in records(root)};approved=set();rejected=set()
    path=Path(path)
    for line in path.read_text(encoding='utf-8').splitlines() if path.exists() else []:
        item=json.loads(line);row=rows.get(item['case_id'])
        if row is None or item['case_sha256']!=sha(encoded(row.model_dump())):raise ValueError('Review case digest mismatch')
        if not item.get('reviewer','').strip() or '请填写' in item['reviewer']:raise ValueError('Actual reviewer declaration required')
        if datetime.fromisoformat(item['reviewed_at']).tzinfo is None:raise ValueError('Timezone required')
        if item['case_id'] in approved|rejected:raise ValueError('Duplicate review decision')
        if item['decision']=='approved':approved.add(row.id)
        elif item['decision']=='needs_change':rejected.add(row.id)
        else:raise ValueError('Invalid review decision')
    rag_ids={r.id for r in rows.values() if isinstance(r,RagCase)}
    return {'independent_human_review_is_self_declared':True,'approved_rag':len(approved&rag_ids),
            'needs_change':len(rejected&rag_ids),'pending_rag':len(rag_ids-(approved|rejected)),
            'gold_gate_passed':rag_ids<=approved,'review_log':str(path)}


async def register(database,root=ROOT,corpus=CORPUS):
    from sqlalchemy import select
    from smart_cs.db.models import DatasetRelease,DatasetSample
    report=validate(root,corpus);root=Path(root)
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    async with database.sessions.begin() as session:
        release=await session.get(DatasetRelease,RELEASE)
        if release and release.manifest_hash!=report['manifest_sha256']:raise ValueError('Registered dataset version is immutable')
        if release is None:
            session.add(DatasetRelease(id=RELEASE,version='v1',manifest_hash=report['manifest_sha256'],provenance='AI-authored source-grounded candidates; no independent human review',manifest=manifest));await session.flush()
        count=0
        for row in records(root):
            identifier=RELEASE+':'+row.id
            if await session.get(DatasetSample,identifier) is None:
                session.add(DatasetSample(id=identifier,release_id=RELEASE,task=row.task,split=row.split,group_id=row.group_id,
                    label=getattr(row,'label',getattr(row,'case_type',getattr(row,'expected_operation',getattr(row,'expected_decision','case')))),payload=row.model_dump()));count+=1
    return {'release_id':RELEASE,'inserted':count,'total_cases':report['total_cases']}
