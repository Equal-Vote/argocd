#!/usr/bin/env python3
"""
keycloak-realm.py — Read-only runbook

Reads realm configuration from the in-cluster Keycloak via its admin REST API.
Authenticates as the bootstrap admin (master realm) using the password from
the `keycloak` secret in the `keycloak` namespace, then GETs the target realm.
The admin REST API is read-only from this script: only HTTP GETs are issued.

Usage:
    python runbook/keycloak-realm.py                      # email-related flags
    python runbook/keycloak-realm.py --field verifyEmail  # one field, raw value
    python runbook/keycloak-realm.py --all                # full realm JSON
    python runbook/keycloak-realm.py --realm master ...   # different realm

The default realm is "Prod" (matches realm.json in the bettervoting repo).
The bootstrap admin username is "user" per the keycloak values.yaml note.
"""

import argparse
import base64
import json
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

NAMESPACE = "keycloak"
SVC = "keycloak"
SVC_PORT = 443
SECRET = "keycloak"
SECRET_KEY = "admin-password"
DEFAULT_ADMIN_USER = "user"
DEFAULT_REALM = "Prod"

EMAIL_FIELDS = [
    "verifyEmail",
    "loginWithEmailAllowed",
    "duplicateEmailsAllowed",
    "registrationAllowed",
    "registrationEmailAsUsername",
    "resetPasswordAllowed",
    "smtpServer",
]


def get_admin_password() -> str:
    out = subprocess.run(
        ["kubectl", "get", "secret", "-n", NAMESPACE, SECRET,
         "-o", f"jsonpath={{.data.{SECRET_KEY}}}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        sys.exit(f"ERROR: secret {SECRET}.{SECRET_KEY} not found in namespace {NAMESPACE}")
    return base64.b64decode(out).decode()


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_port_forward(local_port: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["kubectl", "port-forward", "-n", NAMESPACE,
         f"svc/{SVC}", f"{local_port}:{SVC_PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_for_port(port: int, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    sys.exit("ERROR: port-forward to keycloak service did not become ready")


def http_get(url: str, ctx: ssl.SSLContext, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, context=ctx) as r:
        return r.read()


def http_post_form(url: str, ctx: ssl.SSLContext, data: dict) -> bytes:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, context=ctx) as r:
        return r.read()


def get_admin_token(base: str, ctx: ssl.SSLContext, user: str, password: str) -> str:
    url = f"{base}/realms/master/protocol/openid-connect/token"
    body = http_post_form(url, ctx, {
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": user,
        "password": password,
    })
    return json.loads(body)["access_token"]


def get_realm(base: str, ctx: ssl.SSLContext, token: str, realm: str) -> dict:
    url = f"{base}/admin/realms/{urllib.parse.quote(realm)}"
    body = http_get(url, ctx, {"Authorization": f"Bearer {token}"})
    return json.loads(body)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Read-only Keycloak realm config inspector.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--realm", default=DEFAULT_REALM, help=f"realm name (default: {DEFAULT_REALM})")
    p.add_argument("--admin-user", default=DEFAULT_ADMIN_USER,
                   help=f"master-realm admin username (default: {DEFAULT_ADMIN_USER})")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="dump the full realm JSON")
    g.add_argument("--field", metavar="NAME", help="print one field's raw JSON value")
    args = p.parse_args()

    password = get_admin_password()
    local_port = find_free_port()
    base = f"https://127.0.0.1:{local_port}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # bitnami chart uses an auto-generated self-signed cert

    pf = start_port_forward(local_port)
    try:
        wait_for_port(local_port)
        token = get_admin_token(base, ctx, args.admin_user, password)
        realm = get_realm(base, ctx, token, args.realm)
    finally:
        pf.terminate()
        pf.wait()

    if args.all:
        json.dump(realm, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    if args.field:
        if args.field not in realm:
            sys.exit(f"ERROR: field '{args.field}' not present on realm '{args.realm}'")
        json.dump(realm[args.field], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"Realm: {args.realm}")
    width = max(len(f) for f in EMAIL_FIELDS)
    for f in EMAIL_FIELDS:
        if f in realm:
            print(f"  {f.ljust(width)}  {json.dumps(realm[f])}")
        else:
            print(f"  {f.ljust(width)}  (not set)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
