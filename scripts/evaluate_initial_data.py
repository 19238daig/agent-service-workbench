import argparse
import asyncio
from dotenv import load_dotenv
from smart_cs.settings import ENV_PATH
from smart_cs.evaluation.runner import evaluate

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--split',choices=['train','validation','test'],default='test')
    parser.add_argument('--live-models',action='store_true')
    parser.add_argument('--output')
    args=parser.parse_args();load_dotenv(ENV_PATH)
    report=asyncio.run(evaluate(args.split,args.live_models,output=args.output))
    raise SystemExit(1 if report['errors'] else 0)
