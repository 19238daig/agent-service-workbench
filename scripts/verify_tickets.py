"""Run ticket tests in a temporary PostgreSQL schema with no public writes."""
from verify_auth import main

if __name__=='__main__':raise SystemExit(main('test_tickets'))
