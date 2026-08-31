#!/usr/bin/env python3
"""sync_group_roles.py -- materialize per-member Lakebase roles from a group.

The Lakebase Data API authenticates the INDIVIDUAL identity in the OAuth token
(the token carries no group claim), so a group role cannot be used over the Data
API -- each caller needs its own Postgres role, created with databricks_create_role
and granted to `authenticator`. Doing that by hand per user does not scale.

This script closes that gap with automation: given a Databricks group, it reads
the members, resolves each to its login identity (user email / SP application-id),
and reconciles a per-member Postgres role -- creating roles for new members,
granting them the approved RPC surface, and (with --prune) dropping roles for
members who have left the group. Membership is driven by the group (which is fed
from your IdP via SCIM), so adding/removing a user in the AD group is all that is
needed; run this on a schedule or on a SCIM-change trigger.

This is the Data-API counterpart to the direct-connection group role
(databricks_create_role(<group>,'GROUP')): same "manage the group, not the user"
model, but materialized as individual roles because the Data API needs them.

Run as an OWNER / deploy identity (the ci-deployer role, DATABRICKS_SUPERUSER):
creating roles and granting to `authenticator` requires it.

EDGE CASE (verified): an identity that ALREADY has a Postgres role which was not
created by databricks_create_role -- the project owner, or a user who connected
directly before -- cannot be granted to `authenticator` ("permission denied to
grant role ... only roles with ADMIN option"). Those members are skipped with a
notice rather than failing the batch; for net-new members databricks_create_role
makes a grantable role and provisioning succeeds.

SAFETY
  * Dry-run by default; --apply to make changes; --prune to also drop departed
    members' roles (off by default -- dropping a role fails if it owns objects).
  * Identities are validated (email / UUID shape) before being interpolated into
    role DDL, since role names cannot be bound as query parameters.
  * No credentials are printed. Short-lived OAuth token via the SDK.

Env / args:
  --group           Databricks group display name (required)
  --project-id      Lakebase project id (default: suncorp-claims-center)
  --branch          branch (default: production)
  --database        Postgres database (default: claims_center)
  --schema          schema whose RPCs are granted (default: claims)
  --apply / --prune
  --profile         Databricks CLI/SDK profile
"""
from __future__ import annotations

import argparse
import re
import sys

import psycopg
from databricks.sdk import WorkspaceClient

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# The approved RPC surface granted to each member role (matches sql/003).
RPCS = [
    "rpc_read_memory(text,text,text,text,text)",
    "rpc_write_memory(text,text,text,jsonb,text,text)",
    "rpc_create_task(text,text,text,text,jsonb,text,text)",
    "rpc_complete_task(text,text,text,text,text)",
]


def valid_identity(ident: str) -> bool:
    return bool(EMAIL_RE.match(ident) or UUID_RE.match(ident))


def group_member_identities(w: WorkspaceClient, group_name: str):
    """Return {identity: 'USER'|'SERVICE_PRINCIPAL'} for a group's direct members."""
    groups = [g for g in w.groups.list() if g.display_name == group_name]
    if not groups:
        raise RuntimeError(f"group not found: {group_name!r}")
    full = w.groups.get(groups[0].id)
    out = {}
    for m in (full.members or []):
        kind = (m.ref or "").split("/")[0]
        if kind == "Users":
            u = w.users.get(m.value)
            if u.user_name:
                out[u.user_name] = "USER"
        elif kind == "ServicePrincipals":
            sp = w.service_principals.get(m.value)
            if sp.application_id:
                out[sp.application_id] = "SERVICE_PRINCIPAL"
        # nested groups are skipped; expand here if you use them
    return out


def connect(w: WorkspaceClient, project_id, branch, database):
    parent = f"projects/{project_id}/branches/{branch}"
    ep = next(iter(w.postgres.list_endpoints(parent)))
    host = ep.status.hosts.host
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=ep.name).token
    return psycopg.connect(host=host, port=5432, dbname=database, user=user,
                           password=token, sslmode="require")


def existing_member_roles(cur):
    """Roles already provisioned by this tool = login roles that are members of
    authenticator (that is how a Data-API role is wired), excluding system ones."""
    cur.execute(
        "SELECT r.rolname FROM pg_auth_members m "
        "JOIN pg_roles a ON a.oid=m.member JOIN pg_roles b ON b.oid=m.roleid "
        "WHERE a.rolname='authenticator'"
    )
    return {row[0] for row in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group", required=True)
    ap.add_argument("--project-id", default="suncorp-claims-center")
    ap.add_argument("--branch", default="production")
    ap.add_argument("--database", default="claims_center")
    ap.add_argument("--schema", default="claims")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="also drop roles for members who left the group")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    desired = group_member_identities(w, args.group)
    bad = [i for i in desired if not valid_identity(i)]
    if bad:
        print(f"ERROR: refusing -- identities fail validation: {bad}", file=sys.stderr)
        return 3
    print(f"group {args.group!r}: {len(desired)} member(s)")
    for ident, kind in sorted(desired.items()):
        print(f"  desired: {ident}  ({kind})")

    if not args.apply:
        print("\n[dry-run] no changes. Re-run with --apply (and optionally --prune).")
        return 0

    grant_rpcs = ", ".join(f"{args.schema}.{fn}" for fn in RPCS)
    conn = connect(w, args.project_id, args.branch, args.database)
    conn.autocommit = True
    created = pruned = 0
    skipped = []
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS databricks_auth")
        # provision desired members (idempotent). Each member is its own unit:
        # one problematic identity must not abort the reconcile for the rest.
        for ident, kind in desired.items():
            try:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ident,))
                if not cur.fetchone():
                    # Fresh identity -> create a Data-API-grantable role.
                    cur.execute("SELECT databricks_create_role(%s, %s)", (ident, kind))
                    created += 1
                    print(f"created role {ident} ({kind})")
                # role names cannot be bound; ident is validated above
                cur.execute(f'GRANT "{ident}" TO authenticator')
                cur.execute(f'GRANT USAGE ON SCHEMA {args.schema} TO "{ident}"')
                cur.execute(f'GRANT EXECUTE ON FUNCTION {grant_rpcs} TO "{ident}"')
            except psycopg.errors.InsufficientPrivilege as exc:
                # Happens when the identity ALREADY has a Postgres role that was
                # NOT created by databricks_create_role (e.g. the project owner,
                # or a user who previously connected directly). Such a role
                # cannot be granted to `authenticator`, so it is not usable over
                # the Data API. Skip it and report -- do not fail the batch.
                conn.rollback() if not conn.autocommit else None
                skipped.append((ident, "pre-existing non-managed role"))
                print(f"SKIP {ident}: not Data-API-grantable "
                      f"(existing role not created via databricks_create_role)")
            except Exception as exc:  # noqa: BLE001
                skipped.append((ident, str(exc)[:80]))
                print(f"SKIP {ident}: {str(exc)[:100]}")

        if args.prune:
            # drop roles we manage (authenticator members) that are no longer desired.
            managed = existing_member_roles(cur)
            # never touch the runtime SP(s) or other non-group roles: only prune
            # roles that look like the identities this tool manages (email/uuid)
            for role in managed:
                if role in desired:
                    continue
                if not valid_identity(role):
                    continue
                try:
                    cur.execute(f'REVOKE "{role}" FROM authenticator')
                    cur.execute(f'DROP ROLE "{role}"')
                    pruned += 1
                    print(f"pruned role {role}")
                except Exception as exc:  # noqa: BLE001
                    print(f"WARN: could not drop {role} (owns objects?): {exc}")
    conn.close()
    print(f"\ndone: {created} created, {pruned} pruned, "
          f"{len(desired) - len(skipped)}/{len(desired)} members reconciled.")
    if skipped:
        print(f"skipped {len(skipped)} (see above):")
        for ident, why in skipped:
            print(f"  - {ident}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
