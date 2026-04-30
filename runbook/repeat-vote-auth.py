#!/usr/bin/env python3
"""
repeat-vote-auth.py — Read-only runbook

Explain which auth signals can cause the backend to treat a vote attempt
as a repeat vote for a given election.

This script reads the live election settings from Postgres and maps them
to the backend logic in voterRollUtils.ts / castVoteController.ts.
It does not modify the cluster or the database.

Usage:
    python runbook/repeat-vote-auth.py ELECTION_ID
    python runbook/repeat-vote-auth.py ELECTION_ID --json
"""

import argparse
import base64
import json
import shlex
import subprocess
import sys
from typing import Any, Dict, List

NAMESPACE = "starvote"
PG_POD = "postgresql-0"
SECRET = "star-server"
SECRET_KEY = "DATABASE_URL"
STATEMENT_TIMEOUT = "30s"


def get_database_url() -> str:
    out = subprocess.run(
        [
            "kubectl",
            "get",
            "secret",
            "-n",
            NAMESPACE,
            SECRET,
            "-o",
            f"jsonpath={{.data.{SECRET_KEY}}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not out:
        sys.exit(f"ERROR: secret {SECRET}.{SECRET_KEY} not found in namespace {NAMESPACE}")
    return base64.b64decode(out).decode()


def run_sql_capture(db_url: str, sql: str) -> str:
    pgoptions = (
        f"-c default_transaction_read_only=on "
        f"-c statement_timeout={STATEMENT_TIMEOUT}"
    )
    remote = (
        f'PGOPTIONS={shlex.quote(pgoptions)} '
        f'psql --no-psqlrc -X -v ON_ERROR_STOP=1 -A -t "$DBURL" -f -'
    )
    result = subprocess.run(
        [
            "kubectl",
            "exec",
            "-i",
            "-n",
            NAMESPACE,
            PG_POD,
            "-c",
            "postgresql",
            "--",
            "env",
            f"DBURL={db_url}",
            "sh",
            "-c",
            remote,
        ],
        input=sql,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return result.stdout


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def get_election(db_url: str, election_id: str) -> Dict[str, Any]:
    sql = f"""
SELECT row_to_json(t)
FROM (
    SELECT election_id, title, state, settings
    FROM "electionDB"
    WHERE election_id = {sql_literal(election_id)}
      AND head = true
    LIMIT 1
) t;
"""
    out = run_sql_capture(db_url, sql)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        sys.exit(f"ERROR: election {election_id} not found")
    return json.loads(lines[0])


def bool_flag(settings: Dict[str, Any], key: str) -> bool:
    return settings.get("voter_authentication", {}).get(key) is True


def admin_ui_label(election: Dict[str, Any]) -> str:
    settings = election["settings"]
    voter_access = settings.get("voter_access")
    invitation = settings.get("invitation")
    auth = settings.get("voter_authentication", {})

    enabled = {k for k, v in auth.items() if v is True}
    if voter_access == "open":
        if enabled == {"voter_id"}:
            return "device"
        if enabled == {"email"}:
            return "user (login required)"
        if enabled == {"ip_address"}:
            return "WiFi/cellular network"
        if not enabled:
            return "no limit"
        return "custom/multi-field backend state not representable by the admin radio buttons"

    if voter_access == "closed":
        if invitation == "email" and bool_flag(settings, "voter_id"):
            return "Email List"
        if bool_flag(settings, "voter_id"):
            return "ID List"
        if bool_flag(settings, "email"):
            return "closed election with email-based user matching"
        if bool_flag(settings, "ip_address"):
            return "closed election with IP-based matching"
        return "closed election with custom/no auth flags"

    return "unrecognized / custom configuration"


def explain_match_sources(election: Dict[str, Any]) -> List[str]:
    settings = election["settings"]
    voter_access = settings.get("voter_access")
    auth = settings.get("voter_authentication", {})
    lines: List[str] = []

    if auth.get("voter_id") is True:
        if voter_access == "open":
            lines.append(
                "Uses `voter_id`, sourced from `req.user.sub`."
            )
            lines.append(
                "That `req.user` can come from either a real `id_token` login or a `temp_id` cookie."
            )
            lines.append(
                "In the admin UI this is labeled `device`, but the backend match is really against browser/user identity, not a hardware fingerprint."
            )
        elif voter_access == "closed":
            lines.append(
                "Uses `voter_id`, sourced from the `voter_id` cookie (or an override on bulk/admin ballot upload paths)."
            )
        else:
            lines.append(
                "Uses `voter_id`, but the election's `voter_access` value is unusual, so the exact source should be treated as custom."
            )

    if auth.get("email") is True:
        lines.append(
            "Uses `email`, sourced from `req.user.email`, so the voter needs an authenticated user with an email address."
        )

    if auth.get("ip_address") is True:
        lines.append(
            "Uses `ip_address`, sourced from `req.ip` after hashing it into `electionRollDB.ip_hash`."
        )

    if not lines:
        lines.append(
            "No repeat-vote auth signal is enabled. For open elections this effectively means no persisted identity match, so repeat-vote rejection is not expected."
        )

    return lines


def explain_repeat_vote_behavior(election: Dict[str, Any]) -> List[str]:
    settings = election["settings"]
    voter_access = settings.get("voter_access")
    ballot_updates = settings.get("ballot_updates") is True
    auth = settings.get("voter_authentication", {})
    enabled = [k for k, v in auth.items() if v is True]

    lines: List[str] = []
    if ballot_updates:
        lines.append(
            "Ballot updates are enabled, so a matching prior roll does not trigger the normal `User has already voted` rejection."
        )
    else:
        lines.append(
            "Ballot updates are disabled, so a matching roll with `submitted = true` can trigger `User has already voted`."
        )

    if enabled:
        lines.append(
            "Roll lookup uses OR semantics across enabled auth fields, not AND semantics."
        )
    else:
        lines.append(
            "No auth fields are enabled, so there is no stable lookup key for repeat-vote detection."
        )

    if voter_access == "open":
        lines.append(
            "For open elections, if no matching roll exists, the backend can create a new roll for the current identity."
        )
    else:
        lines.append(
            "For non-open elections, the voter generally must already match an existing roll to proceed."
        )
    return lines


def build_report(election: Dict[str, Any]) -> Dict[str, Any]:
    settings = election["settings"]
    auth = settings.get("voter_authentication", {})
    enabled = [k for k, v in auth.items() if v is True]

    return {
        "election_id": election["election_id"],
        "title": election.get("title"),
        "state": election.get("state"),
        "voter_access": settings.get("voter_access"),
        "invitation": settings.get("invitation"),
        "ballot_updates": settings.get("ballot_updates") is True,
        "enabled_auth_fields": enabled,
        "admin_ui_interpretation": admin_ui_label(election),
        "match_sources": explain_match_sources(election),
        "repeat_vote_behavior": explain_repeat_vote_behavior(election),
        "raw_settings": settings,
    }


def print_text_report(report: Dict[str, Any]) -> None:
    print(f"Election: {report['election_id']}")
    if report.get("title"):
        print(f"Title: {report['title']}")
    print(f"State: {report['state']}")
    print(f"Voter access: {report['voter_access']}")
    print(f"Invitation: {report['invitation']}")
    print(f"Ballot updates enabled: {report['ballot_updates']}")
    print()

    print("Admin UI interpretation:")
    print(f"- {report['admin_ui_interpretation']}")
    print()

    print("Auth fields that can participate in repeat-vote matching:")
    if report["enabled_auth_fields"]:
        for field in report["enabled_auth_fields"]:
            print(f"- {field}")
    else:
        print("- none")
    print()

    print("Where those auth values come from:")
    for line in report["match_sources"]:
        print(f"- {line}")
    print()

    print("Repeat-vote behavior:")
    for line in report["repeat_vote_behavior"]:
        print(f"- {line}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explain repeat-vote auth behavior for a live election.",
    )
    parser.add_argument("election_id", help="Election ID, e.g. bmxpm4")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text",
    )
    args = parser.parse_args()

    db_url = get_database_url()
    election = get_election(db_url, args.election_id)
    report = build_report(election)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
