"""Local operator knowledge ingestion. Metadata JSON describes native source files.

No HTTP uploads, URL fetching, embedding generation or index activation here.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from dotenv import load_dotenv
from pydantic import ValidationError
from sqlalchemy import select,func
from smart_cs.db.database import configured_database
from smart_cs.db.models import KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion
from smart_cs.ingestion.pipeline import IngestionPipeline,IngestionError
from smart_cs.schemas.ingestion import DocumentSpec,IngestionOptions
from smart_cs.settings import ENV_PATH,PROJECT_ROOT


def read_json(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>1024*1024:raise IngestionError('INVALID_MANIFEST','元数据或清单文件不存在或超过 1 MiB。')
    return json.loads(path.read_text(encoding='utf-8-sig'))


async def view(database,identifier=None,limit=50,chunk=None):
    async with database.sessions() as session:
        if identifier:
            document=await session.get(KnowledgeDocument,identifier)
            receipt=await session.scalar(select(KnowledgeIngestion).where(KnowledgeIngestion.document_id==identifier))
            if document is None or receipt is None:raise IngestionError('INGESTED_DOCUMENT_NOT_FOUND','未找到此入库文档。')
            query=select(KnowledgeChunk).where(KnowledgeChunk.document_id==identifier).order_by(KnowledgeChunk.ordinal)
            if chunk:query=query.where(KnowledgeChunk.id==chunk)
            rows=(await session.scalars(query.limit(200))).all()
            return {'document_id':identifier,'title':document.title,'source_key':receipt.source_key,'version':document.version,
                'status':document.status,'access_scope':document.access_scope,'owner_id':document.owner_id,
                'file_path':document.file_path,'metadata':document.metadata_json,
                'chunks':[{'id':c.id,'ordinal':c.ordinal,'page':c.source_page,'heading':c.heading,'locator':c.locator,
                    'embedding_status':c.embedding_status,**({'content':c.content} if chunk else {})} for c in rows]}
        rows=(await session.execute(select(KnowledgeIngestion,KnowledgeDocument).join(KnowledgeDocument,
            KnowledgeDocument.id==KnowledgeIngestion.document_id).order_by(KnowledgeIngestion.created_at.desc()).limit(limit))).all()
        return {'documents':[{'document_id':d.id,'source_key':r.source_key,'title':d.title,'version':d.version,
            'status':d.status,'access_scope':d.access_scope,'owner_id':d.owner_id,'file_path':d.file_path} for r,d in rows]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    for name in ['inspect','import']:
        sub=commands.add_parser(name);sub.add_argument('file');sub.add_argument('--metadata',required=True)
        sub.add_argument('--options',help='Optional IngestionOptions JSON')
        sub.add_argument('--chunker',choices=['structural','bge-semantic'])
        if name=='import':sub.add_argument('--publish',action='store_true',help='Explicitly publish after import; default is draft')
    sub=commands.add_parser('batch');sub.add_argument('manifest');sub.add_argument('--publish',action='store_true')
    sub=commands.add_parser('list');sub.add_argument('--limit',type=int,default=50)
    sub=commands.add_parser('show');sub.add_argument('document_id');sub.add_argument('--chunk',help='Return the selected chunk text too')
    sub=commands.add_parser('publish');sub.add_argument('document_id')
    parser.add_argument('--report',help='Write a JSON report beneath .local/ingestion/')
    args=parser.parse_args(argv);load_dotenv(ENV_PATH)
    result=None
    try:
        if args.action in {'inspect','import'}:
            spec=DocumentSpec.model_validate(read_json(args.metadata));settings=read_json(args.options) if args.options else {}
            if args.chunker:settings['chunker']=args.chunker
            options=IngestionOptions.model_validate(settings)
        if args.action=='inspect':
            prepared=IngestionPipeline().inspect(args.file,spec,options)
            result={k:v for k,v in prepared.items() if k not in {'content','chunks'}}
            result['sample_locators']=[c['locator'] for c in prepared['chunks'][:3]]
        else:
            async def execute():
                db=configured_database()
                if db is None:raise IngestionError('DATABASE_REQUIRED','请先配置根目录 .env 并完成数据库迁移。')
                try:
                    pipeline=IngestionPipeline(db)
                    if args.action=='import':return await pipeline.ingest(args.file,spec,options,publish=args.publish)
                    if args.action=='batch':
                        manifest_path=Path(args.manifest).resolve();manifest=read_json(manifest_path)
                        if set(manifest)!={'documents'} or not isinstance(manifest['documents'],list) or not 1<=len(manifest['documents'])<=100:
                            raise IngestionError('INVALID_MANIFEST','清单仅包含 documents 数组，数量为 1 至 100。')
                        # Validate the whole manifest before the first DB mutation.
                        entries=[]
                        for row in manifest['documents']:
                            if not isinstance(row,dict) or set(row)-{'file','metadata','options'} or not {'file','metadata'}<=set(row):
                                raise IngestionError('INVALID_MANIFEST','每项需要 file 和 metadata，可带 options。')
                            file=(manifest_path.parent/row['file']).resolve()
                            if not file.is_relative_to(manifest_path.parent):raise IngestionError('UNSAFE_MANIFEST_PATH','批量文件必须位于清单所在目录或子目录。')
                            entries.append((file,DocumentSpec.model_validate(row['metadata']),IngestionOptions.model_validate(row.get('options',{}))))
                        results=[]
                        for file,metadata,settings in entries:
                            try:results.append(await pipeline.ingest(file,metadata,settings,publish=args.publish))
                            except Exception as error:
                                return {'passed':False,'results':results,'failed_source_key':metadata.source_key,
                                    'error_code':error.code if isinstance(error,IngestionError) else type(error).__name__,
                                    'transaction_scope':'per_document','previous_documents_retained':True}
                        return {'passed':True,'results':results,'transaction_scope':'per_document'}
                    if args.action=='publish':return await pipeline.publish(args.document_id)
                    if args.action=='list' and not 1<=args.limit<=200:raise IngestionError('INVALID_LIMIT','limit 取值为 1 至 200。')
                    return await view(db,getattr(args,'document_id',None),getattr(args,'limit',50),getattr(args,'chunk',None))
                finally:await db.close()
            result=asyncio.run(execute())
        result={'passed':True,**result}
    except IngestionError as error:result={'passed':False,'error_code':error.code,'message':error.message}
    except ValidationError as error:
        result={'passed':False,'error_code':'INVALID_METADATA','fields':[{'field':'.'.join(map(str,e['loc'])),'type':e['type']} for e in error.errors(include_input=False)]}
    except Exception as error:
        # Driver/parser error bodies can contain secrets or source text.
        result={'passed':False,'error_code':'INGESTION_FAILED','error_type':type(error).__name__,
            'message':'导入未完成，请检查文件格式、编码、清单和数据库迁移。'}
    if args.report:
        target=(PROJECT_ROOT/args.report).resolve()
        if not target.is_relative_to(PROJECT_ROOT/'.local/ingestion'):
            parser.error('Report destination must be beneath .local/ingestion/')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
