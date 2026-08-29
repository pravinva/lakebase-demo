#!/usr/bin/env python3
"""schema_diff.py -- compare the schema of two Lakebase branches.

Used in PR validation: fork production, apply the new migration on the fork with
the schema_deploy job, then diff the fork against production and surface the
delta for review. Promotion still moves the migration file through bundle
targets -- not the branch.

Usage:
  python schema_diff.py --project-id <id> --left <fork> --right production \
      --database appdb
"""
import argparse
import psycopg2
from databricks.sdk import WorkspaceClient


def resolve_rw_endpoint(w, project_id, branch):
    parent = f"projects/{project_id}/branches/{branch}"
    rw = None
    for ep in w.postgres.list_endpoints(parent):
        kind = str(getattr(ep.status, "endpoint_type", "") or "") if ep.status else ""
        if "READ_WRITE" in kind:
            rw = ep
            break
        rw = rw or ep
    if rw is None:
        raise RuntimeError(f"No endpoint found under {parent}")
    host = rw.status.hosts.host if rw.status and rw.status.hosts else None
    return rw.name, host


def columns_for_branch(w, project_id, branch, database, user):
    ep_name, host = resolve_rw_endpoint(w, project_id, branch)
    token = w.postgres.generate_database_credential(endpoint=ep_name).token
    conn = psycopg2.connect(host=host, dbname=database, user=user,
                            password=token, sslmode="require")
    with conn.cursor() as cur:
        cur.execute("SELECT table_schema || '.' || table_name || '.' || column_name "
                    "FROM information_schema.columns "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1;")
        cols = {r[0] for r in cur.fetchall()}
    conn.close()
    return cols


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project-id", required=True)
    p.add_argument("--left", required=True, help="branch with the change (the fork)")
    p.add_argument("--right", default="production", help="baseline branch")
    p.add_argument("--database", default="appdb")
    args = p.parse_args()

    w = WorkspaceClient()
    user = w.current_user.me().user_name
    left = columns_for_branch(w, args.project_id, args.left, args.database, user)
    right = columns_for_branch(w, args.project_id, args.right, args.database, user)

    print(f"=== SCHEMA DIFF: {args.left}  vs  {args.right} ===")
    print(f"columns ONLY in {args.left} (the delta to promote):")
    for c in sorted(left - right):
        print("   +", c)
    print(f"columns only in {args.right}:", sorted(right - left) or "(none)")
    print(f"\n{args.left}: {len(left)} columns | {args.right}: {len(right)} columns")


if __name__ == "__main__":
    main()
