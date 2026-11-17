"""Extend the frozen public snapshot without altering its existing source bytes."""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import shutil
from pathlib import Path
import httpx
from smart_cs.settings import PROJECT_ROOT


def main():
    previous=PROJECT_ROOT/'data/public-after-sales-v1'
    target=PROJECT_ROOT/'data/public-after-sales-v2'
    manifest_path=target/'source_manifest.json'
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        (target/'sources').mkdir(parents=True,exist_ok=True)
        with httpx.Client(trust_env=False,follow_redirects=True,timeout=40) as client:
            for entry in manifest['sources']:
                path=target/'sources'/entry['file']
                if not path.exists():
                    inherited=previous/'sources'/entry['file']
                    if inherited.is_file() and hashlib.sha256(inherited.read_bytes()).hexdigest()==entry['sha256']:
                        raw=inherited.read_bytes()
                    else:
                        response=client.get(entry['url']);response.raise_for_status();raw=response.content
                    if hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                        raise ValueError('Publisher snapshot changed; use the archived version or publish a new corpus')
                    path.write_bytes(raw)
                if hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
                    raise ValueError('Existing public v2 snapshot was modified')
        print('Public v2 snapshot verified: 10 sources.');return
    if not (previous/'source_manifest.json').exists() or not all((previous/'sources'/s['file']).exists() for s in json.loads((previous/'source_manifest.json').read_text(encoding='utf-8'))['sources']):
        from download_public_knowledge import main as download_previous
        download_previous()
    manifest=json.loads((previous/'source_manifest.json').read_text(encoding='utf-8'))
    (target/'sources').mkdir(parents=True,exist_ok=True)
    shutil.copytree(previous/'licenses',target/'licenses',dirs_exist_ok=True)
    for entry in manifest['sources']:
        raw=(previous/'sources'/entry['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==entry['sha256']
        (target/'sources'/entry['file']).write_bytes(raw)
    with httpx.Client(trust_env=False,follow_redirects=True,timeout=40) as client:
        for filename,title,url in [
            ('zammad_find_tickets.html','Zammad Find Tickets','https://user-docs.zammad.org/en/latest/basics/find-tickets.html'),
            ('zammad_macros.html','Zammad Macros','https://user-docs.zammad.org/en/latest/advanced/macros.html')]:
            response=client.get(url);response.raise_for_status()
            assert '<html' in response.text.lower() and len(response.content)<25*1024*1024
            raw=response.content;(target/'sources'/filename).write_bytes(raw)
            manifest['sources'].append({'file':filename,'title':title,'url':url,'resolved_url':str(response.url),
                'publisher':'Zammad documentation','collection':'helpdesk_reference',
                'version':'upstream snapshot 2026-11-13; not this application feature claims',
                'effective_from':'2026-11-13','date_kind':'retrieval_snapshot_not_legal_effective_date',
                'source_type':'public_open_source_documentation','license':'AGPL-3.0',
                'license_url':'https://raw.githubusercontent.com/zammad/zammad-user-documentation/pre-release/LICENSE',
                'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'pages':None,
                'retrieved_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                'policy_scope':'Zammad reference only; not implemented application actions'})
    manifest.update(id='public-after-sales-v2',parent='public-after-sales-v1',
        parent_manifest_sha256=hashlib.sha256((previous/'source_manifest.json').read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (target/'README.md').write_text('# 第二批公开原始资料\n\n共 10 份文件：保留 v1 的 8 份原始字节，新增 Zammad 官方 Find Tickets 与 Macros 两份 HTML。来源和许可见 source_manifest.json、licenses/。\n\n正文与评测题分开：本目录只有公开原文件，不包含问题、参考答案或人工标签。历史中文 PDF 仍为 2020 版本；网页日期不代表法律生效日期。\n',encoding='utf-8')
    print('Prepared public-after-sales-v2: 10 original sources.')


if __name__=='__main__':main()
