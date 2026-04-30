#!/usr/bin/env python3
"""
postgres-ram-history.py — Read-only runbook

Shows PostgreSQL peak memory usage over the past day, week, and month
by querying Prometheus via kubectl port-forward.
No cluster modifications are made.

Usage:
    python runbook/postgres-ram-history.py
"""

import subprocess
import sys
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

NAMESPACE = "starvote"
PROM_NAMESPACE = "kube-prometheus-stack"
PROM_SVC = "kube-prometheus-stack-prometheus"
LOCAL_PORT = 9090
BASE = f"http://localhost:{LOCAL_PORT}"

WINDOWS = [
    ("Past day",   "24h"),
    ("Past week",  "7d"),
    ("Past month", "30d"),
]

# --- show postgresql pods ---
print(f"=== PostgreSQL pods in namespace '{NAMESPACE}' ===")
subprocess.run([
    "kubectl", "get", "pods", "-n", NAMESPACE,
    "-l", "app.kubernetes.io/name=postgresql",
    "-o", "custom-columns="
    "NAME:.metadata.name,"
    "STATUS:.status.phase,"
    "MEM_REQ:.spec.containers[0].resources.requests.memory,"
    "MEM_LIM:.spec.containers[0].resources.limits.memory",
], check=True)
print()

# --- port-forward to prometheus ---
print("Opening port-forward to Prometheus...")
pf = subprocess.Popen(
    ["kubectl", "port-forward", "-n", PROM_NAMESPACE, f"svc/{PROM_SVC}", f"{LOCAL_PORT}:9090"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

try:
    # wait for port-forward
    for _ in range(15):
        try:
            urllib.request.urlopen(f"{BASE}/-/healthy", timeout=1)
            break
        except Exception:
            time.sleep(1)
    else:
        print("ERROR: could not connect to Prometheus", file=sys.stderr)
        sys.exit(1)

    def prom_query(query):
        url = f"{BASE}/api/v1/query?" + urllib.parse.urlencode({"query": query})
        resp = json.loads(urllib.request.urlopen(url).read())
        if resp["status"] != "success":
            print(f"Prometheus error: {resp}", file=sys.stderr)
            sys.exit(1)
        return resp

    def mib(val):
        return round(float(val) / 1048576, 1)

    base_metric = f'container_memory_working_set_bytes{{namespace="{NAMESPACE}", container="postgresql"}}'

    # --- peak usage per window ---
    print("=== Peak PostgreSQL memory usage ===\n")
    print(f"  {'Window':<14} {'Peak (MiB)':>10}   {'Limit (MiB)':>11}   {'% of limit':>10}")
    print(f"  {'-'*14} {'-'*10}   {'-'*11}   {'-'*10}")

    # get the memory limit once
    limit_query = f'kube_pod_container_resource_limits{{namespace="{NAMESPACE}", container="postgresql", resource="memory"}}'
    limit_result = prom_query(limit_query)
    limit_mib = None
    if limit_result["data"]["result"]:
        limit_mib = mib(limit_result["data"]["result"][0]["value"][1])

    for label, window in WINDOWS:
        peak_query = f"max_over_time({base_metric}[{window}])"
        result = prom_query(peak_query)
        if result["data"]["result"]:
            peak = mib(result["data"]["result"][0]["value"][1])
            pct = f"{peak / limit_mib * 100:.1f}%" if limit_mib else "?"
            lim_str = f"{limit_mib}" if limit_mib else "?"
            print(f"  {label:<14} {peak:>10}   {lim_str:>11}   {pct:>10}")
        else:
            print(f"  {label:<14} {'no data':>10}")

    # --- current usage ---
    print()
    current = prom_query(base_metric)
    for r in current["data"]["result"]:
        pod = r["metric"].get("pod", "?")
        cur = mib(r["value"][1])
        pct = f"{cur / limit_mib * 100:.1f}%" if limit_mib else "?"
        print(f"  Current:  {cur} MiB  ({pct} of {limit_mib} MiB limit)  [{pod}]")

    # --- pod status from kubectl ---
    print("\n=== PostgreSQL pod status (from kubectl) ===\n")
    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", NAMESPACE,
         "-l", "app.kubernetes.io/name=postgresql",
         "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    pods = json.loads(result.stdout)
    now = datetime.now(timezone.utc)
    for pod in pods.get("items", []):
        pod_name = pod["metadata"]["name"]
        created = pod["metadata"].get("creationTimestamp", "?")
        if created != "?":
            age = now - datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_str = f"{age.days}d {age.seconds // 3600}h"
        else:
            age_str = "?"
        print(f"  Pod: {pod_name}   Created: {created}   Age: {age_str}")

        for cs in pod.get("status", {}).get("containerStatuses", []):
            restart_count = cs.get("restartCount", 0)
            print(f"    Container: {cs['name']}   Restarts: {restart_count}")

            # current state start time
            running = cs.get("state", {}).get("running")
            if running:
                started = running.get("startedAt", "?")
                if started != "?":
                    up = now - datetime.fromisoformat(started.replace("Z", "+00:00"))
                    up_str = f"{up.days}d {up.seconds // 3600}h"
                else:
                    up_str = "?"
                print(f"    Running since: {started}  (uptime: {up_str})")

            # last termination (only exists if container has restarted)
            last = cs.get("lastState", {}).get("terminated")
            if last:
                reason = last.get("reason", "Unknown")
                exit_code = last.get("exitCode", "?")
                finished = last.get("finishedAt", "?")
                print(f"    Last termination: {reason}" + (" << OOM killed!" if reason == "OOMKilled" else ""))
                print(f"      Exit code: {exit_code}   At: {finished}")
            elif restart_count == 0:
                print(f"    No restarts since pod creation.")
        print()

    # --- restart/OOM history from prometheus ---
    print("=== PostgreSQL restart history (from Prometheus) ===\n")
    for label, window in WINDOWS:
        restarts_query = f'increase(kube_pod_container_status_restarts_total{{namespace="{NAMESPACE}", container="postgresql"}}[{window}])'
        result = prom_query(restarts_query)
        restarts = round(float(result["data"]["result"][0]["value"][1])) if result["data"]["result"] else 0

        oom_query = f'increase(kube_pod_container_status_last_terminated_reason{{namespace="{NAMESPACE}", container="postgresql", reason="OOMKilled"}}[{window}])'
        oom_result = prom_query(oom_query)
        ooms = round(float(oom_result["data"]["result"][0]["value"][1])) if oom_result["data"]["result"] else 0

        print(f"  {label:<14} Restarts: {restarts:<6} OOM kills: {ooms}")

finally:
    pf.terminate()
    pf.wait()
