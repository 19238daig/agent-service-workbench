"""Run order business tests using a temporary PostgreSQL role without public writes."""
from verify_auth import main

if __name__=='__main__':raise SystemExit(main('test_orders'))
