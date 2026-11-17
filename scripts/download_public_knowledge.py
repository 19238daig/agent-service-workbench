"""Download unmodified public PDF/HTML sources; never substitute generated text."""
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import hashlib
import io
import json
import httpx
from pypdf import PdfReader
from smart_cs.settings import PROJECT_ROOT

ROOT=PROJECT_ROOT/'data/public-after-sales-v1'
sources=[
 {'file':'consumer_protection_regulation.pdf','title':'中华人民共和国消费者权益保护法实施条例（司法部原始PDF）',
  'url':'https://xzfg.moj.gov.cn/law/download?LawID=1708&type=pdf','publisher':'中华人民共和国司法部',
  'collection':'consumer_reference','version':'国务院令第778号，2024年公布','effective_from':'2024-07-01','license':'CN-official-legal-text'},
 {'file':'consumer_protection_regulation.html','title':'中华人民共和国消费者权益保护法实施条例（中国政府网HTML）',
  'url':'https://app.www.gov.cn/govdata/gov/202403/19/513111/article.html','publisher':'中国政府网',
  'collection':'consumer_reference','version':'2024-03-19公开页面快照','effective_from':'2024-07-01','license':'CN-official-legal-text'},
 {'file':'seven_day_returns.html','title':'网络购买商品七日无理由退货暂行办法（官方HTML）',
  'url':'https://www.gov.cn/zhengce/zhengceku/2020-11/03/content_5557118.htm','publisher':'中国政府网',
  'collection':'consumer_reference','version':'2020年修订文本页面快照','effective_from':'2020-11-03','license':'CN-official-legal-text','optional':True},
 {'file':'zammad_user_zh_archived.pdf','title':'Zammad中文用户手册（2020历史版本原始PDF）',
  'url':'https://app.readthedocs.org/projects/zammad-user-documentation-chinese/downloads/pdf/latest/',
  'publisher':'Zammad documentation / Read the Docs','collection':'helpdesk_reference',
  'version':'PDF注明2020-09-09；历史参考，不代表当前软件版本','effective_from':'2020-09-09','license':'AGPL-3.0'},
]
for filename,title,path in [
 ('zammad_ticket_basics.html','Zammad Ticket Basics','basics/ticket-basics.html'),
 ('zammad_create_tickets.html','Zammad Create Tickets','basics/create-tickets.html'),
 ('zammad_ticket_actions.html','Zammad Ticket Actions','advanced/ticket-actions.html'),
 ('zammad_knowledge_base.html','Zammad Knowledge Base','extras/knowledge-base.html')]:
 sources.append({'file':filename,'title':title,'url':'https://user-docs.zammad.org/en/latest/'+path,
  'publisher':'Zammad documentation','collection':'helpdesk_reference',
  'version':'upstream latest snapshot 2026-11-13; not this application feature claims',
  'effective_from':'2026-11-13','license':'AGPL-3.0'})


def digest(data):return hashlib.sha256(data).hexdigest()


def main():
 ROOT.mkdir(parents=True,exist_ok=True);(ROOT/'sources').mkdir(exist_ok=True);(ROOT/'licenses').mkdir(exist_ok=True)
 manifest_path=ROOT/'source_manifest.json'
 if manifest_path.exists():
  manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
  with httpx.Client(trust_env=False,follow_redirects=True,timeout=40) as client:
   for item in manifest['sources']:
    destination=ROOT/'sources'/item['file']
    if not destination.exists():
     response=client.get(item['url']);response.raise_for_status()
     if digest(response.content)!=item['sha256']:
      raise ValueError('Publisher content changed; obtain the archived version or create a new corpus version. Existing manifest is not overwritten.')
     destination.write_bytes(response.content)
    if digest(destination.read_bytes())!=item['sha256']:raise ValueError('Existing public snapshot changed; create a new version')
  print('Existing immutable public snapshot verified.');return
 downloaded=[];failed=[]
 with httpx.Client(trust_env=False,follow_redirects=True,timeout=40) as client:
  license_url='https://raw.githubusercontent.com/zammad/zammad-user-documentation/pre-release/LICENSE'
  license_response=client.get(license_url);license_response.raise_for_status()
  if 'AFFERO GENERAL PUBLIC LICENSE' not in license_response.text:raise ValueError('Unexpected Zammad license')
  (ROOT/'licenses/ZAMMAD-LICENSE.txt').write_bytes(license_response.content)
  for source in sources:
   try:
    r=client.get(source['url']);r.raise_for_status()
    if len(r.content)>25*1024*1024:raise ValueError('File too large')
    if source['file'].endswith('.pdf'):
     if not r.content.startswith(b'%PDF-'):raise ValueError('Not an original PDF response')
     pages=len(PdfReader(io.BytesIO(r.content)).pages)
     if pages>200:raise ValueError('PDF page limit exceeded')
    else:
     if '<html' not in r.text.lower():raise ValueError('Not an original HTML response')
     pages=None
    (ROOT/'sources'/source['file']).write_bytes(r.content)
    downloaded.append({**source,'sha256':digest(r.content),'bytes':len(r.content),'pages':pages,
       'retrieved_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'resolved_url':str(r.url),
       'source_type':'public_official_legal_text' if source['collection']=='consumer_reference' else 'public_open_source_documentation',
       'license_url':'https://www.ncac.gov.cn/xxfb/flfg/flfg_532/202103/t20210309_50530.html' if source['collection']=='consumer_reference' else license_url,
       'policy_scope':'public reference only; not merchant-specific promises or proof of implemented features'})
    print('Downloaded original',source['file'],len(r.content),'bytes',pages or '',flush=True)
   except Exception as error:
    failed.append({'url':source['url'],'file':source['file'],'error_type':type(error).__name__})
    if not source.get('optional'):raise
 if sum(s['file'].endswith('.pdf') for s in downloaded)<2:raise ValueError('Two native PDFs required')
 manifest={'id':'public-after-sales-v1','sources':downloaded,'failed_optional_sources':failed,
   'original_bytes_preserved':True,'generated_knowledge':False,
   'license_notes':['Official legal text is recorded separately from website design/assets; retain source and version.',
                    'Zammad documentation retains its upstream AGPL-3.0 notice; not relicensed as project MIT.',
                    'Archived Chinese manual is historical reference; snapshots do not establish current legal applicability.']}
 manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 (ROOT/'README.md').write_text('# 网上公开原始知识语料\n\n本目录实际下载并保留原始 PDF/HTML 字节，不是 AI 编造的业务文件。来源、哈希、日期、版本和许可逐一记录在 source_manifest.json。\n\n消费者参考资料与 Zammad 操作参考分开标注；它们不是商家承诺或本项目已实现功能。中文 Zammad PDF 是2020历史版本，应与当前英文文档区别使用。\n\n原自制 PDF/HTML 仅保留为解析测试，不进入此公开知识索引。原训练/测试集保持冻结，未伪称为网上真实数据集。\n',encoding='utf-8')
 print(json.dumps({'sources':len(downloaded),'pdf_pages':sum(s['pages'] or 0 for s in downloaded),'failed_optional':len(failed),'directory':str(ROOT)},ensure_ascii=False))


if __name__=='__main__':main()
