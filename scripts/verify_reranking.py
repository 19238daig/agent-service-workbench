"""Run reranking contracts and authorization cases in a restricted PG schema."""
from verify_auth import main

if __name__ == '__main__':
    raise SystemExit(main('test_reranking'))
