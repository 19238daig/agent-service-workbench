"""Start/check core infrastructure without resetting data or calling paid models."""
import argparse
import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv
from smart_cs.settings import ENV_PATH,PROJECT_ROOT
from smart_cs.services.dependencies import DependencyChecker


def main():
    load_dotenv(ENV_PATH)
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['start','check'])
    parser.add_argument('--deep',action='store_true',help='Actually test local embedding/reranking and Redis TTL')
    parser.add_argument('--runtime-only',action='store_true',help='Skip host Docker/WSL commands and local model-file checks (e.g. in CI)')
    args=parser.parse_args()
    output=PROJECT_ROOT/'.local/infrastructure';output.mkdir(parents=True,exist_ok=True)
    try:
        if args.action=='start':
            from smart_cs.foundation import ensure_services
            ensure_services()
            if DependencyChecker().rag_enabled:
                from smart_cs.retrieval.runtime import ensure_runtime
                ensure_runtime()
        report=asyncio.run(DependencyChecker().run(deep=args.deep,include_host=not args.runtime_only))
        data=report.model_dump()
        name=('runtime-' if args.runtime_only else '')+('deep-report.json' if args.deep else 'connectivity-report.json')
        (output/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (output/'latest.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(data,ensure_ascii=False,indent=2))
        return 0 if report.ready else 1
    except Exception as error:
        from datetime import datetime,timezone
        from smart_cs.schemas.dependencies import DependencyReport,DependencyCheck
        safe=DependencyReport(checked_at=datetime.now(timezone.utc).isoformat(),profile='deep' if args.deep else 'connectivity',
            ready=False,host_checks_included=not args.runtime_only,checks={'startup':DependencyCheck(status='failed',required=True,
            code='INFRASTRUCTURE_START_FAILED',details={'error_type':type(error).__name__,
            'message':'Check Docker Desktop, root configuration and .local/rag logs; existing data was not reset.'})}).model_dump()
        (output/'last-error.json').write_text(json.dumps(safe,indent=2)+'\n',encoding='utf-8')
        (output/'latest.json').write_text(json.dumps(safe,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(safe,ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
