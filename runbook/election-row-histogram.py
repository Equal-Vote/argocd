#!/usr/bin/env python3
"""
election-row-histogram.py — Read-only runbook

Reports the distribution of append-only history rows per election in
"electionDB". Every settings edit inserts a new row (old head flipped
to false), so this is the metric to watch if autosave-heavy admin flows
start producing runaway per-election histories.

All work is a single GROUP BY on the PK prefix + a few aggregates, so
it's one sequential scan + hash aggregate. At current DB size it's
well under a second. Guarded by default_transaction_read_only=on and
statement_timeout=30s.

Usage:
    python runbook/election-row-histogram.py
"""

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
    pgoptions = (
        f"-c default_transaction_read_only=on "
        f"-c statement_timeout={STATEMENT_TIMEOUT}"
    )
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


SQL = r"""
\echo === electionDB summary ===
SELECT
    COUNT(*) AS total_rows,
    COUNT(DISTINCT election_id) AS total_elections,
    COUNT(*) FILTER (WHERE head) AS head_rows,
    COUNT(*) FILTER (WHERE NOT head) AS history_rows,
    pg_size_pretty(pg_total_relation_size('"electionDB"')) AS on_disk
FROM "electionDB";

\echo
\echo === Rows-per-election distribution ===
WITH per_election AS (
    SELECT election_id, COUNT(*)::bigint AS rows
    FROM "electionDB"
    GROUP BY election_id
)
SELECT
    round(avg(rows)::numeric, 2) AS mean,
    percentile_disc(0.5)  WITHIN GROUP (ORDER BY rows) AS p50,
    percentile_disc(0.9)  WITHIN GROUP (ORDER BY rows) AS p90,
    percentile_disc(0.99) WITHIN GROUP (ORDER BY rows) AS p99,
    max(rows) AS max
FROM per_election;

\echo
\echo === Histogram (elections bucketed by row count) ===
WITH per_election AS (
    SELECT election_id, COUNT(*)::bigint AS rows
    FROM "electionDB"
    GROUP BY election_id
),
bucketed AS (
    SELECT CASE
        WHEN rows = 1                    THEN '1  [1]'
        WHEN rows BETWEEN 2   AND 5      THEN '2  [2-5]'
        WHEN rows BETWEEN 6   AND 10     THEN '3  [6-10]'
        WHEN rows BETWEEN 11  AND 25     THEN '4  [11-25]'
        WHEN rows BETWEEN 26  AND 50     THEN '5  [26-50]'
        WHEN rows BETWEEN 51  AND 100    THEN '6  [51-100]'
        WHEN rows BETWEEN 101 AND 500    THEN '7  [101-500]'
        ELSE                                  '8  [500+]'
    END AS bucket
    FROM per_election
)
SELECT bucket,
       COUNT(*) AS elections,
       repeat(
           '#',
           GREATEST(1, (COUNT(*) * 50 / MAX(COUNT(*)) OVER ()))::int
       ) AS bar
FROM bucketed
GROUP BY bucket
ORDER BY bucket;

\echo
\echo === Top 20 elections by row count ===
SELECT election_id, COUNT(*) AS rows
FROM "electionDB"
GROUP BY election_id
ORDER BY COUNT(*) DESC
LIMIT 20;
"""


def main() -> int:
    db_url = get_database_url()
    return run_sql(db_url, SQL)


if __name__ == "__main__":
    sys.exit(main())
