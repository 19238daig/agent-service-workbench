"""Prepare a native-file import manifest for a NEW empty knowledge database.

Copies already downloaded, hash-verified public bytes; never creates FAQ prose.
On an existing installation use rag_cli index directly instead.
"""
import argparse
import hashlib
import json
from pathlib import Path
from smart_cs.settings import PROJECT_ROOT
from smart_cs.schemas.ingestion import DocumentSpec


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',default='data/public-after-sales-v2')
    parser.add_argument('--output',default='.local/public-ingestion-v2');args=parser.parse_args()
    corpus=(PROJECT_ROOT/args.corpus).resolve();output=(PROJECT_ROOT/args.output).resolve()
    if not corpus.is_relative_to(PROJECT_ROOT/'data') or not output.is_relative_to(PROJECT_ROOT/'.local'):parser.error('Use the project data corpus and a .local output folder')
    source_manifest=corpus/'source_manifest.json';manifest=json.loads(source_manifest.read_text(encoding='utf-8'))
    if manifest.get('generated_knowledge') is not False:raise ValueError('Verified native public sources required')
    entries=[];raw_files=[]
    for entry in manifest['sources']:
        name=entry['file']
        if Path(name).name!=name:raise ValueError('Unsafe source name')
        source=(corpus/'sources'/name).resolve()
        if not source.is_relative_to(corpus/'sources'):raise ValueError('Unsafe source path')
        raw=source.read_bytes()
        if len(raw)>25*1024*1024 or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('Public source checksum mismatch')
        spec=DocumentSpec(source_key='public-native:'+name,title=entry['title'],version=entry['version'][:32],domain=entry['collection'],
            access_scope='public',effective_from=entry['effective_from'],source_type=entry['source_type'],publisher=entry['publisher'],
            license=entry['license'],source_url=entry['url'],expected_sha256=entry['sha256'])
        entries.append({'file':'sources/'+name,'metadata':spec.model_dump(mode='json',exclude_none=True),'options':{'chunker':'bge-semantic'}})
        raw_files.append((name,raw))
    encoded=json.dumps({'documents':entries},ensure_ascii=False,indent=2)+'\n'
    destination=output/'manifest.json'
    if destination.exists() and destination.read_text(encoding='utf-8')!=encoded:raise ValueError('Existing import plan differs; choose a new output folder')
    (output/'sources').mkdir(parents=True,exist_ok=True)
    for name,raw in raw_files:
        target=output/'sources'/name
        if target.exists() and target.read_bytes()!=raw:raise ValueError('Existing copied source was modified')
        if not target.exists():target.write_bytes(raw)
    destination.write_text(encoded,encoding='utf-8')
    print(json.dumps({'passed':True,'documents':len(entries),'manifest':destination.relative_to(PROJECT_ROOT).as_posix(),
        'source_manifest_sha256':hashlib.sha256(source_manifest.read_bytes()).hexdigest(),'database_modified':False,'for_new_empty_knowledge_database':True},indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
