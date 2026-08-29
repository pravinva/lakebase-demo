#!/usr/bin/env python3
"""deploy_schema.py -- ordered, versioned Lakebase migration runner.

Runs on serverless as the `schema_deploy` job's task (or locally). Resolves the
branch's read-write endpoint, mints a short-lived OAuth token, and applies the
migration files in src/migrations in filename order. A schema_version table
records what has run, so each file is applied once per database/branch.

Migrations may contain {{name}} tokens (e.g. service-principal ids in
V3__roles_and_grants.sql); pass them with --var name=value. A file whose tokens
are not all supplied (non-empty) is skipped with a notice, so the schema-only
migrations run without needing runtime identity ids.

Serverless notes (both handled below):
  * __file__ is undefined under a spark_python_task -> resolve dirs from cwd.
  * sys.exit(0) reads as a task failure -> call main() directly.
Pin databricks-sdk>=0.96 in the job env (serverless ships an older SDK without
w.postgres) alongside psycopg2-binary.
"""
import argparse, glob, os, pathlib, re
import psycopg2
from databricks.sdk import WorkspaceClient

TOKEN_RE = re.compile(r"\{\{(\w+)\}\}")


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
        raise RuntimeError(f"No endpoint found under {parent} (check the branch name)")
    host = rw.status.hosts.host if rw.status and rw.status.hosts else None
    if not host:
        raise RuntimeError(f"Endpoint {rw.name} has no host yet")
    return rw.name, host


def resolve_migrations_dir(rel):
    for base in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]:
        if glob.glob(os.path.join(base / rel, "V*__*.sql")):
            return str(base / rel)
    raise RuntimeError(f"{rel} not found from {pathlib.Path.cwd()}")


def substitute(sql, variables):
    """Replace {{name}} with variables[name]. Returns (text, applicable):
    applicable is False when the file has tokens that aren't all non-empty."""
    tokens = set(TOKEN_RE.findall(sql))
    if not tokens:
        return sql, True
    if any(not variables.get(t) for t in tokens):
        return sql, False
    return TOKEN_RE.sub(lambda m: variables[m.group(1)], sql), True


def ensure_version_table(cur):
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_version ("
        "  version TEXT PRIMARY KEY,"
        "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


def already_applied(cur, version):
    cur.execute("SELECT 1 FROM public.schema_version WHERE version = %s", (version,))
    return cur.fetchone() is not None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project-id", required=True)
    p.add_argument("--database", default="appdb")
    p.add_argument("--branch", default="production")
    p.add_argument("--migrations-dir", default="src/migrations")
    p.add_argument("--var", action="append", default=[], metavar="name=value",
                   help="value for a {{name}} token in a migration (repeatable)")
    args = p.parse_args()
    variables = dict(kv.split("=", 1) for kv in args.var if "=" in kv)

    w = WorkspaceClient()
    user = w.current_user.me().user_name
    ep_name, host = resolve_rw_endpoint(w, args.project_id, args.branch)
    token = w.postgres.generate_database_credential(endpoint=ep_name).token
    conn = psycopg2.connect(host=host, dbname=args.database, user=user,
                            password=token, sslmode="require")
    conn.autocommit = True

    mig_dir = resolve_migrations_dir(args.migrations_dir)
    files = sorted(glob.glob(os.path.join(mig_dir, "V*__*.sql")))
    print(f"migrations dir: {mig_dir}  ({len(files)} files)", flush=True)
    if not files:
        raise RuntimeError(f"No V*__*.sql under {mig_dir}")

    applied, skipped = 0, 0
    try:
        with conn.cursor() as cur:
            ensure_version_table(cur)
            for path in files:
                version = os.path.basename(path)
                if already_applied(cur, version):
                    print(f"skip {version} (already applied)", flush=True); skipped += 1; continue
                sql, applicable = substitute(open(path).read(), variables)
                if not applicable:
                    print(f"skip {version} (tokens not supplied via --var)", flush=True); skipped += 1; continue
                print(f"apply {version} ...", flush=True)
                cur.execute(sql)                       # one implicit txn per file
                cur.execute("INSERT INTO public.schema_version(version) VALUES (%s)", (version,))
                applied += 1
    finally:
        conn.close()
    print(f"done: {applied} applied, {skipped} skipped", flush=True)


if __name__ == "__main__":
    # Call main() directly: under a serverless spark_python_task SystemExit reads
    # as a task failure even at code 0.
    main()
