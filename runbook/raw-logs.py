#!/usr/bin/env python3
"""
raw-logs.py — Pull raw logs from Loki, time-linearized across all streams.

Usage:
    python runbook/raw-logs.py [QUERY] [MINUTES] [LIMIT]

    QUERY    LogQL selector (default: {service_name="app"})
    MINUTES  How far back to look (default: 10)
    LIMIT    Max lines to fetch (default: 100)
"""

import subprocess
import sys
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

QUERY = sys.argv[1] if len(sys.argv) > 1 else '{service_name="app"}'
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 10
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 100

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

    now = datetime.now(timezone.utc)
    start_ns = str(int((now - timedelta(minutes=MINUTES)).timestamp() * 1e9))
    end_ns = str(int(now.timestamp() * 1e9))

    print(f"\nQuery: {QUERY}")
    print(f"Window: last {MINUTES} min, limit {LIMIT} lines\n")

    params = urllib.parse.urlencode({
        "query": QUERY,
        "start": start_ns,
        "end": end_ns,
        "limit": LIMIT,
        "direction": "backward",
    })
    resp = json.loads(urllib.request.urlopen(f"{BASE}/loki/api/v1/query_range?{params}").read())

    # collect all lines across all streams, then sort by timestamp
    lines = []
    for stream in resp["data"]["result"]:
        instance = stream["stream"].get("app_kubernetes_io_instance", "")
        pod = stream["stream"].get("pod", stream["stream"].get("statefulset_kubernetes_io_pod_name", "?"))
        for ts_ns, raw in stream["values"]:
            ts = int(ts_ns) / 1e9
            try:
                log_line = json.loads(raw)["log"]
            except (json.JSONDecodeError, KeyError):
                log_line = raw
            lines.append((ts, instance, pod, log_line.rstrip()))

    lines.sort()

    for ts, instance, pod, line in lines:
        t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        print(f"[{t}] [{instance}/{pod}] {line}")

    print(f"\n({len(lines)} lines)")

finally:
    pf.terminate()
    pf.wait()
