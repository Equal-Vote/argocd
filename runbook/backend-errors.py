#!/usr/bin/env python3
"""
backend-errors.py — Read-only runbook

Queries Loki for backend (star-server) error and warning logs,
grouped into events. Log lines within 100ms of each other from the
same pod are treated as a single event.
No cluster modifications are made.

Usage:
    python runbook/backend-errors.py [HOURS]

    HOURS  Number of hours to look back (default: 24)
"""

import subprocess
import sys
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24
LOKI_NAMESPACE = "loki"
LOKI_SVC = "loki"
LOCAL_PORT = 3100
BASE = f"http://localhost:{LOCAL_PORT}"

# How close in time (seconds) log lines must be to group into one event
GROUP_GAP = 0.1

# --- port-forward to loki ---
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

    now = datetime.now(timezone.utc)
    start_ns = str(int((now - timedelta(hours=HOURS)).timestamp() * 1e9))
    end_ns = str(int(now.timestamp() * 1e9))

    def loki_query_range(logql, limit=5000, start=start_ns, end=end_ns):
        params = urllib.parse.urlencode({
            "query": logql,
            "start": start,
            "end": end,
            "limit": limit,
            "direction": "backward",
        })
        url = f"{BASE}/loki/api/v1/query_range?{params}"
        resp = json.loads(urllib.request.urlopen(url).read())
        if resp["status"] != "success":
            print(f"Loki error: {resp}", file=sys.stderr)
            sys.exit(1)
        return resp

    def extract_entries(result):
        """Parse Loki results into sorted (ts_float, pod, log_line) tuples."""
        entries = []
        for stream in result["data"]["result"]:
            pod = stream["stream"].get("pod", "?")
            for ts_ns, raw in stream["values"]:
                ts = int(ts_ns) / 1e9
                try:
                    log_line = json.loads(raw)["log"]
                except (json.JSONDecodeError, KeyError):
                    log_line = raw
                entries.append((ts, pod, log_line.rstrip()))
        entries.sort()
        return entries

    def group_events(entries):
        """Group consecutive lines from the same pod within GROUP_GAP seconds."""
        if not entries:
            return []
        events = []
        current = [entries[0]]
        for entry in entries[1:]:
            prev_ts, prev_pod, _ = current[-1]
            ts, pod, _ = entry
            if pod == prev_pod and (ts - prev_ts) <= GROUP_GAP:
                current.append(entry)
            else:
                events.append(current)
                current = [entry]
        events.append(current)
        return events

    # How many seconds of context to fetch around each error event
    CONTEXT_WINDOW = 2.0

    def fetch_context(events):
        """For each event, fetch all log lines from the same pod in a window around it."""
        contextualized = []
        for event in events:
            pod = event[0][1]
            event_start = event[0][0]
            event_end = event[-1][0]
            window_start = str(int((event_start - CONTEXT_WINDOW) * 1e9))
            window_end = str(int((event_end + CONTEXT_WINDOW) * 1e9))
            ctx_query = f'{{service_name="app", pod="{pod}"}}'
            ctx_result = loki_query_range(ctx_query, limit=200, start=window_start, end=window_end)
            ctx_entries = extract_entries(ctx_result)
            contextualized.append((event, ctx_entries))
        return contextualized

    def print_events(events, with_context=False):
        if with_context:
            contextualized = fetch_context(events)
        for i, item in enumerate(contextualized if with_context else events):
            if with_context:
                event, ctx_entries = item
            else:
                event = item
            ts = datetime.fromtimestamp(event[0][0], tz=timezone.utc)
            pod = event[0][1]
            print(f"  --- event {i+1}  [{ts.strftime('%Y-%m-%d %H:%M:%S')}]  [{pod}] ---")
            if with_context and ctx_entries:
                # highlight the matching lines
                match_times = {e[0] for e in event}
                for ts_f, _, line in ctx_entries:
                    marker = ">>>" if ts_f in match_times else "   "
                    print(f"    {marker} {line}")
            else:
                for _, _, line in event:
                    print(f"    {line}")
            print()

    selector = '{service_name="app"}'

    # --- explicit errors (Logger.error calls) ---
    print(f"\n=== Backend ERRORS (last {HOURS}h) ===\n")
    error_result = loki_query_range(f'{selector} | json | log =~ ".*ERROR.*"')
    errors = extract_entries(error_result)
    error_events = group_events(errors)
    if error_events:
        print(f"  {len(error_events)} error events ({len(errors)} log lines):\n")
        print_events(error_events, with_context=True)
    else:
        print("  No errors found.")

    # --- HTTP 5xx responses ---
    print(f"=== Backend HTTP 5xx (last {HOURS}h) ===\n")
    http5_result = loki_query_range(f'{selector} | json | log =~ ".*status:5\\\\d+.*"')
    http5 = extract_entries(http5_result)
    http5_events = group_events(http5)
    if http5_events:
        print(f"  {len(http5_events)} events ({len(http5)} log lines):\n")
        print_events(http5_events, with_context=True)
    else:
        print("  No 5xx responses found.")

    # --- HTTP 4xx responses ---
    print(f"=== Backend HTTP 4xx (last {HOURS}h) ===\n")
    http4_result = loki_query_range(f'{selector} | json | log =~ ".*status:4\\\\d+.*"')
    http4 = extract_entries(http4_result)
    http4_events = group_events(http4)
    if http4_events:
        # summarize by status code
        from collections import Counter
        import re
        status_counts = Counter()
        for ts, pod, line in http4:
            m = re.search(r"status:(\d+)", line)
            if m:
                status_counts[m.group(1)] += 1
        print(f"  {len(http4_events)} events ({len(http4)} log lines):")
        print(f"  Breakdown: {', '.join(f'{code}: {n}' for code, n in sorted(status_counts.items()))}\n")
        print_events(http4_events)
    else:
        print("  No 4xx responses found.")

    # --- warnings ---
    print(f"=== Backend WARNINGS (last {HOURS}h) ===\n")
    warn_result = loki_query_range(f'{selector} | json | log =~ ".*WARN.*"')
    warnings = extract_entries(warn_result)
    warn_events = group_events(warnings)
    if warn_events:
        print(f"  {len(warn_events)} warning events ({len(warnings)} log lines):\n")
        print_events(warn_events)
    else:
        print("  No warnings found.")

    # --- summary ---
    print(f"=== Summary (last {HOURS}h) ===")
    print(f"  Error events:   {len(error_events)}")
    print(f"  HTTP 5xx events: {len(http5_events)}")
    print(f"  HTTP 4xx events: {len(http4_events)}")
    print(f"  Warning events: {len(warn_events)}")

finally:
    pf.terminate()
    pf.wait()
