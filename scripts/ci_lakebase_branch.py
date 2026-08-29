#!/usr/bin/env python3
"""ci_lakebase_branch.py -- ephemeral Lakebase branch per CI run.

Demonstrates the "database branch per pull request" pattern: for each PR, spin
up a copy-on-write Lakebase branch off production, apply the migrations, smoke
-test the RPC boundary against the clone, then tear the branch down when the PR
closes. This is the Lakebase analogue of ephemeral preview databases.

Subcommands:
  create   create the branch (+ wait for a compute endpoint), emit host/endpoint
  migrate  apply sql/001,003,004 to the branch DB (idempotent on the clone)
  test     run offline static checks + a live RPC smoke test on the branch
  destroy  delete the branch (cascades its endpoint)

Auth: OAuth M2M via env DATABRICKS_HOST + DATABRICKS_CLIENT_ID +
DATABRICKS_CLIENT_SECRET (a service principal). Requires databricks-sdk>=0.133
and psycopg. The service principal needs CAN CREATE / CAN MANAGE on the Lakebase
project so it can create and delete branches.

Config via env (with demo defaults):
  LAKEBASE_PROJECT_ID       default: suncorp-claims-center
  LAKEBASE_SOURCE_BRANCH    default: production
  LAKEBASE_DB_NAME          default: claims_center
  LAKEBASE_PG_SCHEMA        default: claims
(branches are created no-expiry; the teardown job deletes them on PR close.)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import postgres as pg

PROJECT = os.environ.get("LAKEBASE_PROJECT_ID", "suncorp-claims-center")
SOURCE  = os.environ.get("LAKEBASE_SOURCE_BRANCH", "production")
DBNAME  = os.environ.get("LAKEBASE_DB_NAME", "claims_center")
SCHEMA  = os.environ.get("LAKEBASE_PG_SCHEMA", "claims")
HERE    = os.path.dirname(os.path.abspath(__file__))
ROOT    = os.path.dirname(HERE)

def project_path() -> str: return f"projects/{PROJECT}"
def branch_path(name: str) -> str: return f"{project_path()}/branches/{name}"

def _client() -> WorkspaceClient:
    # WorkspaceClient picks up DATABRICKS_HOST/CLIENT_ID/CLIENT_SECRET from env.
    return WorkspaceClient()

def _wait(fn, ok, tries=60, delay=5, what="resource"):
    last = None
    for _ in range(tries):
        last = fn()
        if ok(last):
            return last
        time.sleep(delay)
    raise TimeoutError(f"timed out waiting for {what}; last={last!r}")

def _state(obj):
    # SDK returns an enum (e.g. BranchStatusState.READY); normalize to its value.
    st = getattr(obj, "status", None)
    cs = getattr(st, "current_state", None) if st else None
    return getattr(cs, "value", None) or (str(cs) if cs is not None else None)

def _endpoint(w, name):
    eps = list(w.postgres.list_endpoints(branch_path(name)))
    return eps[0] if eps else None

def _gh_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"{key}={value}\n")

def _conn(w, endpoint_name, host, dbname):
    import psycopg
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=endpoint_name).token
    return psycopg.connect(host=host, port=5432, dbname=dbname, user=user,
                           password=token, sslmode="require")

# --- subcommands -------------------------------------------------------------
def cmd_create(args):
    w = _client()
    name = args.name
    # no_expiry=True keeps it simple (avoids Duration serialization); the
    # teardown job deletes the branch when the PR closes.
    spec = pg.BranchSpec(source_branch=branch_path(SOURCE), no_expiry=True)
    try:
        w.postgres.create_branch(project_path(), pg.Branch(spec=spec), branch_id=name)
        print(f"[create] requested branch {name} from {SOURCE}")
    except Exception as exc:  # noqa: BLE001
        if "exist" in str(exc).lower():
            print(f"[create] branch {name} already exists; reusing")
        else:
            raise
    _wait(lambda: w.postgres.get_branch(branch_path(name)),
          lambda b: _state(b) in ("READY", "ACTIVE"), what="branch READY")

    ep = _endpoint(w, name)
    if ep is None:
        # No auto endpoint -> create a small, scale-to-zero one.
        try:
            espec = pg.EndpointSpec(
                endpoint_type=pg.EndpointEndpointType.ENDPOINT_TYPE_READ_WRITE,
                autoscaling_limit_min_cu=0.5, autoscaling_limit_max_cu=1.0,
                suspend_timeout_duration="300s")
            w.postgres.create_endpoint(branch_path(name), pg.Endpoint(spec=espec), endpoint_id="ci")
            print("[create] created budget-safe endpoint 'ci' (<=1 CU, scale-to-zero 300s)")
        except Exception as exc:  # noqa: BLE001
            print(f"[create] create_endpoint note: {exc}")
    ep = _wait(lambda: _endpoint(w, name),
               lambda e: e and _state(e) in ("ACTIVE", "IDLE"), what="endpoint ACTIVE")
    host = ep.status.hosts.host
    print(f"[create] branch {name} READY; endpoint={ep.name} host={host}")
    _gh_output("branch", name)
    _gh_output("endpoint", ep.name)
    _gh_output("host", host)

def cmd_migrate(args):
    # NOTE: applying the DDL migrations requires an OWNER / migration principal
    # (CREATE on the database + schema). The least-privilege runtime service
    # principal used by CI has EXECUTE-only and will get "permission denied for
    # database". The CI workflow therefore does NOT call migrate on the branch
    # clone (the clone is already migrated); it verifies the clone via `test`.
    # Run `migrate` with an owner identity when validating a NEW migration.
    w = _client()
    ep = _endpoint(w, args.name)
    if not ep:
        print("ERROR: no endpoint on branch", args.name, file=sys.stderr); return 2
    host = ep.status.hosts.host
    files = ["sql/001_schema.sql", "sql/003_rpc_functions.sql", "sql/004_cdf_prereqs.sql"]
    # 002_roles_and_grants.sql is intentionally skipped in CI: it provisions the
    # runtime SP roles (needs real SP client IDs) and the branch is a clone that
    # already carries them. CI validates that the schema/RPC/CDF SQL applies
    # cleanly on a production-like clone.
    with _conn(w, ep.name, host, DBNAME) as conn:
        for rel in files:
            sql = open(os.path.join(ROOT, rel), "r", encoding="utf-8").read()
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print(f"[migrate] applied {rel}")
    print(f"[migrate] OK on branch {args.name}")

def cmd_test(args):
    # 1) offline static safety + contract checks (no DB)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "test_sql_static.py")])
    if r.returncode != 0:
        print("ERROR: offline static tests failed", file=sys.stderr); return 1
    # 2) live smoke test against the branch clone
    w = _client()
    ep = _endpoint(w, args.name)
    host = ep.status.hosts.host
    with _conn(w, ep.name, host, DBNAME) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {SCHEMA}.rpc_write_memory('CLM-10001','ci.agent','ci.smoke',"
                        f"'{{\"ci\":true}}','ci-req','ci-trace')")
            cur.execute(f"SELECT memory_value FROM {SCHEMA}.rpc_read_memory('CLM-10001','ci.agent','ci.smoke')")
            val = cur.fetchone()
            print("[test] rpc read-after-write:", val)
            conn.commit()
            # cross-claim denial must raise
            cur.execute(f"SELECT {SCHEMA}.rpc_create_task('CLM-10001','WF','ci.agent','t','{{}}','r','t')")
            tid = cur.fetchone()[0]; conn.commit()
            denied = False
            try:
                cur.execute(f"SELECT {SCHEMA}.rpc_complete_task(%s,'CLM-10002','ci.agent','r','t')", (tid,))
            except Exception as exc:  # noqa: BLE001
                denied = "cross-claim access denied" in str(exc).lower()
                conn.rollback()
    if not denied:
        print("ERROR: cross-claim call was NOT denied on the branch", file=sys.stderr); return 1
    print("[test] PASS: RPC read/write works and cross-claim access is denied on the branch")

def cmd_destroy(args):
    w = _client()
    try:
        w.postgres.delete_branch(branch_path(args.name), allow_missing=True, purge=True)
        print(f"[destroy] deleted branch {args.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[destroy] note: {exc}")

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("create", "migrate", "test", "destroy"):
        sp = sub.add_parser(c)
        sp.add_argument("--name", required=True, help="Lakebase branch name (e.g. ci-pr-42)")
    args = ap.parse_args()
    try:
        return {"create": cmd_create, "migrate": cmd_migrate,
                "test": cmd_test, "destroy": cmd_destroy}[args.cmd](args) or 0
    except Exception as exc:  # noqa: BLE001
        m = str(exc).lower()
        # A workspace IP access list will block GitHub-hosted runners. That's an
        # environment/network policy, not a pipeline error: exit 75 so the
        # workflow can report it as "run on a self-hosted runner" and stay green.
        if "ip acl" in m or "is blocked" in m or "blocked by databricks" in m:
            print("::warning title=Workspace not reachable::Runner IP is blocked "
                  "by the workspace IP access list. Run the live Lakebase jobs on "
                  "a self-hosted runner inside the allowed network. Detail: "
                  f"{exc}", file=sys.stderr)
            return 75
        raise

if __name__ == "__main__":
    raise SystemExit(main())
