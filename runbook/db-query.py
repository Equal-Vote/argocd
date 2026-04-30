#!/usr/bin/env python3
"""
db-query.py — Read-only runbook

Runs read-only SQL against the star-server PostgreSQL database.
Credentials are read from the star-server DATABASE_URL secret in the
starvote namespace; psql is invoked inside the postgresql-0 pod so no
port-forward is needed. All queries run with
  default_transaction_read_only=on
  statement_timeout=30s
so nothing can be modified and runaway queries are bounded.

Usage:
    python runbook/db-query.py                     # summary: tables + row counts
    python runbook/db-query.py -c "SELECT ..."     # run read-only SQL
    python runbook/db-query.py -f query.sql        # run SQL from a file
    python runbook/db-query.py --describe TABLE    # show columns for a table
    python runbook/db-query.py --tables            # list tables only

Examples — investigating the SendGrid webhook "no sent row" warning:
    python runbook/db-query.py --describe emailEventsDB
    python runbook/db-query.py -c "SELECT event_type, COUNT(*) FROM \\"emailEventsDB\\" \\
        WHERE event_timestamp > now() - interval '1 hour' GROUP BY 1 ORDER BY 2 DESC;"
    python runbook/db-query.py -c "SELECT * FROM \\"emailEventsDB\\" \\
        WHERE message_id = 'Cgn9VOciTmKWBU8Ec3mfqw.recvd-5756697cd6-qp4jz-1-69E90327-D.0';"
"""

import argparse
import base64
import shlex
import subprocess
import sys

NAMESPACE = "starvote"
PG_POD = "postgresql-0"
SECRET = "star-server"
SECRET_KEY = "DATABASE_URL"
STATEMENT_TIMEOUT = "30s"


def get_database_url() -> str:
    out = subprocess.run(
        ["kubectl", "get", "secret", "-n", NAMESPACE, SECRET,
         "-o", f"jsonpath={{.data.{SECRET_KEY}}}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        sys.exit(f"ERROR: secret {SECRET}.{SECRET_KEY} not found in namespace {NAMESPACE}")
    return base64.b64decode(out).decode()


def run_sql(db_url: str, sql: str) -> int:
    """Pipe SQL into psql inside the postgres pod. Read-only + timeout enforced."""
    pgoptions = (
        f"-c default_transaction_read_only=on "
        f"-c statement_timeout={STATEMENT_TIMEOUT}"
    )
    # psql reads from stdin via `-f -`. We quote DBURL on the remote side to
    # avoid word-splitting on passwords containing shell metacharacters.
    remote = (
        f'PGOPTIONS={shlex.quote(pgoptions)} '
        f'psql --no-psqlrc -X -v ON_ERROR_STOP=1 "$DBURL" -f -'
    )
    result = subprocess.run(
        ["kubectl", "exec", "-i", "-n", NAMESPACE, PG_POD, "-c", "postgresql", "--",
         "env", f"DBURL={db_url}", "sh", "-c", remote],
        input=sql, text=True,
    )
    return result.returncode


def default_summary(db_url: str) -> int:
    sql = r"""
\echo === Tables in public schema ===
\dt
\echo
\echo === Row counts (approximate, via pg_class.reltuples) ===
SELECT c.relname AS table, c.reltuples::bigint AS approx_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.reltuples DESC;
\echo
\echo === Database size ===
SELECT pg_size_pretty(pg_database_size(current_database())) AS size;
"""
    return run_sql(db_url, sql)


def describe(db_url: str, table: str) -> int:
    # Use quoted identifier so case-sensitive names like emailEventsDB work.
    quoted = '"' + table.replace('"', '""') + '"'
    sql = f"\\d {quoted}\n"
    return run_sql(db_url, sql)


def list_tables(db_url: str) -> int:
    return run_sql(db_url, "\\dt\n")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Read-only SQL runbook for the star-server Postgres DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("-c", "--command", help="SQL to execute")
    g.add_argument("-f", "--file", help="Path to a .sql file to execute")
    g.add_argument("--describe", metavar="TABLE", help="Show columns/indexes for a table")
    g.add_argument("--tables", action="store_true", help="List tables and exit")
    args = p.parse_args()

    db_url = get_database_url()

    if args.command:
        return run_sql(db_url, args.command)
    if args.file:
        with open(args.file) as fh:
            return run_sql(db_url, fh.read())
    if args.describe:
        return describe(db_url, args.describe)
    if args.tables:
        return list_tables(db_url)
    return default_summary(db_url)


if __name__ == "__main__":
    sys.exit(main())
