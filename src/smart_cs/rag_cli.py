"""Operate immutable dense indexes. Querying returns evidence, not generated text."""
import argparse
import asyncio
import json
from dotenv import load_dotenv
from sqlalchemy import select
from smart_cs.settings import ENV_PATH,PROJECT_ROOT
from smart_cs.db.database import configured_database
from smart_cs.db.models import RetrievalIndex
from smart_cs.schemas.errors import ApplicationError


def main(argv=None):
    load_dotenv(ENV_PATH)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start-models','index','status','indexes','activate','search'])
    parser.add_argument('--activate',action='store_true',help='Explicitly activate a completed newly built index')
    parser.add_argument('--index-id');parser.add_argument('--expected-active')
    parser.add_argument('--query');parser.add_argument('--top-k',type=int,default=5)
    parser.add_argument('--sources',help='Legacy argument: import source files with ingestion.cli instead')
    parser.add_argument('--report',help='JSON path beneath .local/retrieval/')
    args=parser.parse_args(argv)
    if args.sources:parser.error('Use ingestion.cli to import native files; index uses published database chunks')
    report_path=(PROJECT_ROOT/args.report).resolve() if args.report else None
    if report_path and not report_path.is_relative_to(PROJECT_ROOT/'.local/retrieval'):parser.error('Report must be beneath .local/retrieval/')
    try:
        if args.action=='start-models':
            from smart_cs.retrieval.runtime import ensure_runtime
            ensure_runtime();result={'passed':True,'local_models_ready':True}
        else:
            async def execute():
                db=configured_database()
                if db is None:raise ApplicationError('DATABASE_REQUIRED','请先配置根目录数据库。',503)
                try:
                    from smart_cs.retrieval.indexes import DenseIndexBuilder
                    if args.action=='index':return await DenseIndexBuilder(db).build(activate=args.activate)
                    if args.action=='activate':
                        if not args.index_id:raise ApplicationError('INDEX_ID_REQUIRED','请指定 --index-id。',400)
                        return await DenseIndexBuilder(db).activate(args.index_id,expected_active=args.expected_active)
                    if args.action=='indexes':
                        async with db.sessions() as session:
                            rows=(await session.scalars(select(RetrievalIndex).order_by(RetrievalIndex.created_at.desc()))).all()
                            return {'indexes':[{'index_id':r.id,'active':r.active,'embedding':r.model_identity.get('embedding'),
                                'protocol':r.configuration.get('protocol','legacy'),'chunks':r.configuration.get('chunks')} for r in rows]}
                    if args.action=='search':
                        from smart_cs.retrieval.dense import DenseRetriever
                        return await DenseRetriever(db).search(args.query or '',args.top_k)
                    from smart_cs.memory.bge_memory import BGEKnowledgeMemory
                    return await BGEKnowledgeMemory(db).describe()
                finally:await db.close()
            result={'passed':True,**asyncio.run(execute())}
    except ApplicationError as error:result={'passed':False,'error_code':error.code,'message':error.message}
    except Exception as error:result={'passed':False,'error_code':'RETRIEVAL_OPERATION_FAILED','error_type':type(error).__name__,
        'message':'操作未完成，请检查本地模型服务、原文件、数据库和迁移；当前索引未被自动回退。'}
    if report_path:
        report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
