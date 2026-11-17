import argparse
import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv
from smart_cs.settings import ENV_PATH,PROJECT_ROOT
from smart_cs.evaluation.assets import build,validate,register,review_status
from smart_cs.db.database import configured_database


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['build','validate','register','review-status'])
    parser.add_argument('--reviews',default='.local/evaluation/reviews-v1.jsonl')
    args=parser.parse_args();load_dotenv(ENV_PATH)
    if args.action=='build':result=build()
    elif args.action=='validate':result=validate()
    elif args.action=='review-status':result=review_status(PROJECT_ROOT/args.reviews)
    else:
        async def run():
            database=configured_database()
            if database is None:raise RuntimeError('Database required')
            try:return await register(database)
            finally:await database.close()
        result=asyncio.run(run())
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
