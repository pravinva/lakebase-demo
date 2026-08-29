#!/usr/bin/env python3
"""apply_sql.py -- opt-in migration runner for the Suncorp claims-center demo.

Applies one reviewed .sql file to the Lakebase Autoscaling database using a
short-lived Lakebase OAuth database credential minted through the Databricks
CLI. Dry-run by default; pass --apply to execute.

SAFETY
  * Dry-run by default. Nothing is executed without --apply.
  * REFUSES to apply any file that still contains an unresolved angle-bracket
    placeholder such as <UIPATH_SP_CLIENT_ID>.
  * The OAuth credential is short-lived (~1h) and is never written to disk.
  * No credential is ever printed.

Environment (see README step 1):
  LAKEBASE_ENDPOINT   projects/<project>/branches/<branch>/endpoints/<endpoint>
  LAKEBASE_DB_NAME    target Postgres database name (default: claims_center)
  DATABRICKS_CONFIG_PROFILE   CLI profile to use (or pass --profile)

Requires: databricks CLI on PATH, psycopg (v3).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# Matches unresolved placeholders like <UIPATH_SP_CLIENT_ID>, <CLAIMS_OWNER_ROLE>.
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")


def _cli(args: list[str], profile: str) -> dict | list:
    """Run a databricks CLI command with -o json and return parsed output."""
    cmd = ["databricks", *args, "--profile", profile, "-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"databricks {' '.join(args)} failed: {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _endpoint_host(endpoint_path: str, profile: str) -> str:
    """Resolve the connection host from the endpoint's branch."""
    # endpoint_path = projects/<p>/branches/<b>/endpoints/<e>
    branch_path = endpoint_path.split("/endpoints/")[0]
    endpoints = _cli(["postgres", "list-endpoints", branch_path], profile)
    for ep in endpoints:
        host = ep.get("status", {}).get("hosts", {}).get("host")
        if host:
            return host
    raise RuntimeError(f"no host found for branch {branch_path}")


def _oauth_token(endpoint_path: str, profile: str) -> str:
    cred = _cli(
        ["postgres", "generate-database-credential", endpoint_path], profile
    )
    token = cred.get("token")
    if not token:
        raise RuntimeError("generate-database-credential returned no token")
    return token


def _current_user(profile: str) -> str:
    me = _cli(["current-user", "me"], profile)
    return me["userName"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, help="path to the .sql file")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually execute (default: dry-run / validate only)",
    )
    ap.add_argument(
        "--profile",
        default=os.environ.get("DATABRICKS_CONFIG_PROFILE", "DEFAULT"),
        help="Databricks CLI profile (default: $DATABRICKS_CONFIG_PROFILE or DEFAULT)",
    )
    ap.add_argument(
        "--endpoint",
        default=os.environ.get("LAKEBASE_ENDPOINT"),
        help="endpoint resource path (default: $LAKEBASE_ENDPOINT)",
    )
    ap.add_argument(
        "--dbname",
        default=os.environ.get("LAKEBASE_DB_NAME", "claims_center"),
        help="target database name (default: $LAKEBASE_DB_NAME or claims_center)",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        print(f"ERROR: file not found: {args.file}", file=sys.stderr)
        return 2

    with open(args.file, "r", encoding="utf-8") as fh:
        sql_text = fh.read()

    # --- Placeholder guard: refuse to apply unresolved placeholders. ---------
    placeholders = sorted(set(PLACEHOLDER_RE.findall(sql_text)))
    if placeholders:
        print(
            "ERROR: refusing to apply -- unresolved placeholders remain:\n  "
            + "\n  ".join(placeholders),
            file=sys.stderr,
        )
        print(
            "\nReplace them (e.g. the real service-principal client IDs) and "
            "re-run.",
            file=sys.stderr,
        )
        return 3

    if not args.apply:
        stmts = [s for s in sql_text.split(";") if s.strip()]
        print(f"[dry-run] {args.file}: OK -- no unresolved placeholders.")
        print(f"[dry-run] ~{len(stmts)} statement(s) would be executed.")
        print("[dry-run] re-run with --apply to execute.")
        return 0

    if not args.endpoint:
        print(
            "ERROR: --endpoint or $LAKEBASE_ENDPOINT is required to --apply.",
            file=sys.stderr,
        )
        return 2

    try:
        import psycopg  # noqa: F401  (import here so dry-run needs no driver)
    except ImportError:
        print(
            "ERROR: psycopg (v3) is required to --apply. `pip install psycopg[binary]`.",
            file=sys.stderr,
        )
        return 2

    host = _endpoint_host(args.endpoint, args.profile)
    token = _oauth_token(args.endpoint, args.profile)
    user = _current_user(args.profile)

    print(f"[apply] connecting to {host}/{args.dbname} as {user} ...")
    conninfo = (
        f"host={host} port=5432 dbname={args.dbname} "
        f"user={user} sslmode=require"
    )
    # A single transaction: the whole file applies atomically or not at all.
    with psycopg.connect(conninfo, password=token, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
        conn.commit()

    print(f"[apply] {args.file}: applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
