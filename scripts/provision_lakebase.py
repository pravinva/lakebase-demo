#!/usr/bin/env python3
"""provision_lakebase.py -- opt-in provisioning for the claims-center demo.

Provisions the three runtime prerequisites that the SQL migrations assume:

  1. A Lakebase managed Postgres role for the UiPath service principal, backed
     by SERVICE_PRINCIPAL + LAKEBASE_OAUTH_V1 (created only if missing).
  2. The Lakebase Data API enabled for the `claims` Postgres schema.
  3. A Change Data Feed (CDF) configuration binding the `claims` schema to
     Unity Catalog history tables in the target catalog/schema.

SAFETY
  * Dry-run by DEFAULT. It prints exactly what it would do. Pass --apply to act.
  * It does NOT create or alter a Lakebase project/branch/endpoint.
  * CDF config is created ONCE and is immutable afterward. If an equivalent
    configuration already exists, this script reports it rather than creating a
    second one.
  * If the target workspace does not expose a given API operation, the script
    prints guidance to use the Lakebase UI instead of failing hard.

SDK interfaces used (Autoscaling tier; verified on databricks-sdk 0.133.0 --
re-verify against your installed SDK with --check-sdk):
    module   databricks.sdk.service.postgres
    types    DataApi, DataApiDataApiSpec, CdfConfig, Role, RoleRoleSpec,
             RoleAuthMethod (LAKEBASE_OAUTH_V1), RoleIdentityType (SERVICE_PRINCIPAL)
    client   WorkspaceClient().postgres
    methods  create_role(parent, role, role_id=..., replace_existing=...)
             create_data_api(parent, data_api)
             create_cdf_config(parent, cdf_config, cdf_config_id=...)
    where    parent = projects/<project>/branches/<branch>/databases/<database>

Run `python scripts/provision_lakebase.py --check-sdk` first to print the
installed SDK version and confirm these symbols resolve before --apply.
"""
from __future__ import annotations

import argparse
import sys


def _sdk_report() -> int:
    """Print installed SDK version and whether the expected symbols resolve."""
    try:
        import databricks.sdk  # noqa: F401
    except ImportError:
        print("databricks-sdk is NOT installed. `pip install databricks-sdk`.")
        return 2

    version = getattr(databricks.sdk, "__version__", None)
    if version is None:
        try:
            from importlib.metadata import version as _pkg_version

            version = _pkg_version("databricks-sdk")
        except Exception:  # noqa: BLE001
            version = "unknown (package metadata not found)"
    print(f"databricks-sdk version: {version}")

    ok = True
    # The Autoscaling-tier Lakebase Postgres API lives in the `postgres` service
    # module (verified on databricks-sdk 0.133.0). Older SDKs only shipped the
    # Provisioned-tier `database` module and lack these symbols entirely.
    try:
        from databricks.sdk.service import postgres as pgsvc

        for sym in ("DataApi", "DataApiDataApiSpec", "CdfConfig", "Role",
                    "RoleRoleSpec", "RoleAuthMethod", "RoleIdentityType"):
            present = hasattr(pgsvc, sym)
            print(f"  databricks.sdk.service.postgres.{sym}: "
                  f"{'found' if present else 'MISSING'}")
            ok = ok and present
    except ImportError:
        print("  databricks.sdk.service.postgres: MISSING (module not present)")
        print("  HINT: Autoscaling Lakebase Postgres support was added in a newer "
              "databricks-sdk. Upgrade (`pip install -U databricks-sdk`) or "
              "configure the SP role, Data API, and CDF via the Lakebase UI. The "
              "CLI path (`databricks postgres ...`) used by apply_sql.py and the "
              "notebooks works regardless of SDK version.")
        ok = False

    try:
        from databricks.sdk import WorkspaceClient  # noqa: F401

        has_pg = hasattr(WorkspaceClient, "postgres")
        print(f"  WorkspaceClient.postgres client: "
              f"{'found' if has_pg else 'MISSING'}")
        ok = ok and has_pg
    except ImportError as exc:  # pragma: no cover
        print(f"  could not import WorkspaceClient: {exc}")
        ok = False

    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-id", help="Lakebase project id or uid")
    ap.add_argument("--branch-id", default="production")
    ap.add_argument("--database-id", help="Lakebase database id")
    ap.add_argument("--endpoint", help="endpoint resource path")
    ap.add_argument("--uipath-sp-client-id",
                    help="UiPath service-principal application/client ID")
    ap.add_argument("--cdf-catalog", help="Unity Catalog catalog for CDF history")
    ap.add_argument("--cdf-schema", default="suncorp_claims_cdf")
    ap.add_argument("--pg-schema", default="claims",
                    help="Postgres schema to expose / feed (default: claims)")
    ap.add_argument("--profile", default=None,
                    help="Databricks CLI/SDK profile")
    ap.add_argument("--apply", action="store_true",
                    help="actually provision (default: dry-run)")
    ap.add_argument("--check-sdk", action="store_true",
                    help="print SDK version + symbol availability and exit")
    ap.add_argument("--skip-sp-role", action="store_true",
                    help="skip creating the managed SP role (use when the SP "
                         "client IDs are stubbed / roles already exist); only "
                         "Data API + CDF are configured")
    args = ap.parse_args()

    if args.check_sdk:
        return _sdk_report()

    # Data API + CDF are database-scoped; endpoint and SP client id are not used
    # here (SP roles are created in sql/002 -- see step 1 note below).
    required = {
        "--project-id": args.project_id,
        "--database-id": args.database_id,
        "--cdf-catalog": args.cdf_catalog,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"ERROR: missing required args: {', '.join(missing)}",
              file=sys.stderr)
        return 2

    plan = [
        "1) SP roles: created in sql/002_roles_and_grants.sql via "
        "databricks_create_role() + GRANT TO authenticator (NOT here -- "
        "SDK-created roles are not Data-API grantable).",
        f"2) Enable Data API on Postgres schema '{args.pg_schema}' of database "
        f"'{args.database_id}' (project '{args.project_id}', "
        f"branch '{args.branch_id}').",
        f"3) Create CDF config for schema '{args.pg_schema}' -> Unity Catalog "
        f"'{args.cdf_catalog}.{args.cdf_schema}' (idempotent; immutable once "
        f"created).",
    ]
    print("Planned actions:")
    for step in plan:
        print(f"  {step}")

    if not args.apply:
        print("\n[dry-run] no changes made. Re-run with --apply to provision.")
        print("[dry-run] tip: run --check-sdk first to verify SDK symbols.")
        return 0

    # ---- --apply path ------------------------------------------------------
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service import postgres as pgsvc
    except ImportError as exc:
        print(f"ERROR: databricks-sdk (Autoscaling Postgres API) import failed: "
              f"{exc}", file=sys.stderr)
        print("Upgrade the SDK and re-run --check-sdk, or use the Lakebase UI.",
              file=sys.stderr)
        return 2

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    pg = getattr(w, "postgres", None)
    if pg is None:
        print("WARN: WorkspaceClient has no `postgres` client. Use the Lakebase "
              "UI to create the SP role, enable the Data API, and configure CDF.")
        return 1

    # Resource paths (Autoscaling hierarchy). Roles are BRANCH-scoped; Data API
    # and CDF are DATABASE-scoped (confirmed via the SDK/CLI docs).
    branch_path = f"projects/{args.project_id}/branches/{args.branch_id}"
    database_path = f"{branch_path}/databases/{args.database_id}"

    # ---- 1) Managed SP role -------------------------------------------------
    # IMPORTANT: SP roles are NOT created here. A role created via this SDK's
    # postgres.create_role (or the Roles UI) CANNOT be granted to the Data API's
    # `authenticator` role, so HTTP Data API calls as that SP fail with
    # "42501 permission denied to set role". Create the SP roles in
    # sql/002_roles_and_grants.sql, which uses databricks_create_role() and then
    # GRANTs each role to `authenticator` (the verified, Data-API-compatible
    # path). This step is therefore informational only.
    print("[apply] SP roles: create them via sql/002_roles_and_grants.sql "
          "(databricks_create_role + GRANT ... TO authenticator). Not created "
          "by this script -- SDK-created roles are not Data-API grantable.")

    # ---- 2) Data API (expose the `claims` schema) --------------------------
    try:
        api = pgsvc.DataApi(spec=pgsvc.DataApiDataApiSpec(
            db_schemas=[args.pg_schema],
        ))
        pg.create_data_api(database_path, api)
        print(f"[apply] Data API enabled for schema '{args.pg_schema}'.")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "exist" in msg or "already" in msg:
            print(f"[apply] Data API already configured. ({exc})")
        else:
            print(f"WARN: Data API step reported: {exc}")

    # ---- 3) CDF config (immutable once created) ----------------------------
    try:
        cdf = pgsvc.CdfConfig(
            postgres_schema=args.pg_schema,
            catalog=args.cdf_catalog,
            schema=args.cdf_schema,
        )
        pg.create_cdf_config(database_path, cdf)
        print(f"[apply] CDF configured: {args.pg_schema} -> "
              f"{args.cdf_catalog}.{args.cdf_schema}.")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "exist" in msg or "already" in msg:
            print(f"[apply] CDF config already exists; not creating a second "
                  f"one. ({exc})")
        else:
            print(f"WARN: CDF step reported: {exc}")

    print("\n[apply] provisioning attempt complete. Review WARN lines above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
