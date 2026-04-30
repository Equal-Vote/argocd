#!/usr/bin/env python3
"""
ctx-trace.py — Read-only runbook

Fetches all log lines matching a context ID, plus a surrounding time
window from the same pod. No cluster modifications are made.

Usage:
    python runbook/ctx-trace.py SEARCH_TERM [SEARCH_HOURS] [WINDOW_MINUTES]

    SEARCH_TERM     e.g. "ctx:fe37d504", "fe37d504", or "h:631a51e39fef"
    SEARCH_HOURS    how far back to search (default: 24)
    WINDOW_MINUTES  context around the match (default: 3)
"""

import subprocess
import sys
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

if len(sys.argv) < 2:
    print("Usage: python runbook/ctx-trace.py SEARCH_TERM [WINDOW_MINUTES]")
    sys.exit(1)

SEARCH_TERM = sys.argv[1]
# accept raw IDs, ctx:xxx, or h:xxx formats
if ":" not in SEARCH_TERM:
    SEARCH_TERM = f"ctx:{SEARCH_TERM}"
SEARCH_HOURS = float(sys.argv[2]) if len(sys.argv) > 2 else 24
WINDOW_MIN = float(sys.argv[3]) if len(sys.argv) > 3 else 3

LOKI_NAMESPACE = "loki"
LOKI_SVC = "loki"
LOCAL_PORT = 3100
BASE = f"http://localhost:{LOCAL_PORT}"

print("Opening port-forward to Loki...")
pf = subprocess.Popen(
    ["kubectl", "port-forward", "-n", LOKI_NAMESPACE, f"svc/{LOKI_SVC}", f"{LOCAL_PORT}:3100"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

try:
    for _ in range(15):
        try:
            urllib.request.urlopen(f"{BASE}/ready", timeout=1)
            break
        except Exception:
            time.sleep(1)
    else:
        print("ERROR: could not connect to Loki", file=sys.stderr)
        sys.exit(1)

    def loki_query_range(logql, start_ns, end_ns, limit=5000):
        params = urllib.parse.urlencode({
            "query": logql,
            "start": start_ns,
            "end": end_ns,
            "limit": limit,
            "direction": "forward",
        })
        url = f"{BASE}/loki/api/v1/query_range?{params}"
        resp = json.loads(urllib.request.urlopen(url).read())
        if resp["status"] != "success":
            print(f"Loki error: {resp}", file=sys.stderr)
            sys.exit(1)
        return resp

    def extract_entries(result):
        entries = []
        for stream in result["data"]["result"]:
            for ts_ns, raw in stream["values"]:
                ts = int(ts_ns) / 1e9
                try:
                    log_line = json.loads(raw)["log"]
                except (json.JSONDecodeError, KeyError):
                    log_line = raw
                entries.append((ts, log_line.rstrip()))
        entries.sort()
        return entries

    # Step 1: find all lines with this ctx ID (search last 28 days — loki retention)
    print(f"\nSearching for {SEARCH_TERM} (last {SEARCH_HOURS}h)...")
    now = datetime.now(timezone.utc)
    search_start = str(int((now - timedelta(hours=SEARCH_HOURS)).timestamp() * 1e9))
    search_end = str(int(now.timestamp() * 1e9))

    ctx_result = loki_query_range(
        f'{{service_name="app"}} |= "{SEARCH_TERM}"',
        search_start, search_end, limit=500,
    )
    ctx_entries = extract_entries(ctx_result)

    if not ctx_entries:
        print(f"  No log lines found for {SEARCH_TERM}")
        sys.exit(0)

    first_ts = ctx_entries[0][0]
    last_ts = ctx_entries[-1][0]
    print(f"  Found {len(ctx_entries)} lines spanning "
          f"{datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} to "
          f"{datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 2: fetch all logs in a window around those timestamps
    window_start = str(int((first_ts - WINDOW_MIN * 60) * 1e9))
    window_end = str(int((last_ts + WINDOW_MIN * 60) * 1e9))

    print(f"  Fetching {WINDOW_MIN}min context window...\n")
    all_result = loki_query_range(
        '{service_name="app"}',
        window_start, window_end, limit=5000,
    )
    all_entries = extract_entries(all_result)

    # Print with matching lines highlighted
    ctx_times = {e[0] for e in ctx_entries}
    ctx_id_short = SEARCH_TERM.split(":")[1]
    for ts, line in all_entries:
        t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        if ts in ctx_times:
            print(f"  >>> [{t}] {line}")
        elif ctx_id_short in line:
            print(f"  >>> [{t}] {line}")
        else:
            print(f"      [{t}] {line}")

    print(f"\n({len(all_entries)} total lines, {len(ctx_entries)} matched {SEARCH_TERM})")

finally:
    pf.terminate()
    pf.wait()
