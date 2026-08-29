# Suncorp claims-center · Lakebase agent-memory demo

Starter package for a synthetic Suncorp claims workflow in which **Databricks
agents and UiPath agents share claim-scoped memory in Lakebase Autoscaling**.

## What this package demonstrates

- UiPath authenticates to the Lakebase Data API with a Databricks OAuth bearer token.
- A UiPath service principal is represented by a Lakebase Postgres role backed by `SERVICE_PRINCIPAL` and `LAKEBASE_OAUTH_V1`.
- Runtime identities call approved RPC functions rather than directly reading or mutating memory tables.
- Each logical agent operation writes an append-only application audit event.
- Lakebase CDF exposes row changes for the audit schema as Unity Catalog Delta history tables.
- A Databricks agent and a UiPath agent can continue the same claim workflow from shared state.
- A cross-claim access attempt is denied.

## Safety boundary

This is a **non-production starter**. It contains synthetic data and
placeholders only. It does **not** create or alter a Lakebase project by
default, does **not** contain credentials, and does **not** include destructive
SQL. Review the SQL with Suncorp security and database owners before applying.

The migration SQL assumes an administrative / project-owner connection. The
UiPath runtime principal receives `USAGE` and `EXECUTE` on approved objects, not
direct DML on the memory, task, or audit tables.

## Package layout

| Path | Purpose |
|------|---------|
| `sql/001_schema.sql` | schema, tables, indexes, append-only audit trigger, RLS baseline |
| `sql/002_roles_and_grants.sql` | role placeholders and least-privilege grants |
| `sql/003_rpc_functions.sql` | SECURITY DEFINER RPCs for read/write/task operations |
| `sql/004_cdf_prereqs.sql` | `REPLICA IDENTITY FULL` prerequisites + verification queries |
| `scripts/provision_lakebase.py` | opt-in SDK provisioning: SP role, Data API, CDF |
| `scripts/apply_sql.py` | opt-in migration runner using a short-lived Lakebase OAuth credential |
| `scripts/test_data_api.sh` | UiPath-style Data API calls and negative tests |
| `notebooks/01_seed_synthetic_claims.py` | seed synthetic claims, agents, memory, tasks |
| `notebooks/02_verify_agent_audit_and_cdf.py` | demo timeline; audit reads vs CDF writes |
| `tests/test_sql_static.py` | offline safety and contract checks |
| `genie_code_prompts/` | prompts for Genie Code to review, adapt, and validate |

## Object model (at a glance)

- `claims.claim` — claim → owning `business_unit` (the cross-claim boundary).
- `claims.agent` — logical agents: `uipath.document_review.v1`, `databricks.claim_triage.v1`.
- `claims.agent_memory` — claim-scoped `(claim_id, memory_key) → JSONB` shared state.
- `claims.task` — claim/workflow-scoped tasks.
- `claims.audit_event` — **append-only** application audit; records the authenticated
  `principal` (session_user) **and** the caller-asserted logical `agent_id`, for every
  operation including reads.

The four RPCs — `rpc_read_memory`, `rpc_write_memory`, `rpc_create_task`,
`rpc_complete_task` — are the only surface granted to runtime principals.

## Prerequisites

- Lakebase Autoscaling project, branch, database, and a read-write endpoint.
- Databricks CLI/SDK compatible with the target workspace. Check first:
  `databricks --version` and `python scripts/provision_lakebase.py --check-sdk`.
- A project owner or delegated DBA for schema/function migrations.
- A UiPath service-principal **client ID** (the application/client ID, not the display name).
- A Unity Catalog catalog/schema where CDF history tables may be materialized.
- `psycopg` (v3) for the migration/seed scripts: `pip install "psycopg[binary]"`.

> **Workspace / profile.** Point these scripts at your Lakebase-enabled
> Databricks workspace through a Databricks CLI profile. Set
> `DATABRICKS_CONFIG_PROFILE=<your-profile>` (or pass `--profile <your-profile>`)
> so `apply_sql.py`, the notebooks, and the CLI all target the same workspace.

> **SDK note (verified on `databricks-sdk` 0.133.0).** The Autoscaling Data API
> / CDF / managed-role SDK symbols live in **`databricks.sdk.service.postgres`**
> (client `WorkspaceClient().postgres`), not the older `database` module. They
> require a recent SDK — on an older one, `--check-sdk` reports them MISSING;
> upgrade the SDK or configure the SP role, Data API, and CDF via the Lakebase
> UI. Credential minting and endpoint lookup use the CLI (`databricks postgres
> ...`), which works regardless of SDK version. A local venv keeps this off the
> system Python: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## 1. Configure environment variables

```bash
export DATABRICKS_CONFIG_PROFILE='<your-profile>'   # your Lakebase workspace CLI profile
export LAKEBASE_PROJECT_ID='<project-id-or-uid>'
export LAKEBASE_BRANCH_ID='production'
export LAKEBASE_DATABASE_ID='<database-id>'
export LAKEBASE_ENDPOINT='projects/<project-id>/branches/<branch-id>/endpoints/<endpoint-id>'
export LAKEBASE_DB_NAME='claims_center'         # Postgres database holding the `claims` schema
export LAKEBASE_PG_SCHEMA='claims'
export UIPATH_SP_CLIENT_ID='<uipath-service-principal-client-id>'
export DATABRICKS_CDF_CATALOG='<standard-uc-catalog>'
export DATABRICKS_CDF_SCHEMA='suncorp_claims_cdf'
```

The database resource path is
`projects/<project-id>/branches/<branch-id>/databases/<database-id>`. Use the
project UID if the workspace API requires it; do not infer it from a display name.

## 2. Review and apply schema migrations

Review every SQL file first. Before applying `002_roles_and_grants.sql`, replace:
`<UIPATH_SP_CLIENT_ID>`, `<DATABRICKS_AGENT_SP_CLIENT_ID>` (the SP **application/
client IDs**), and `<CLAIMS_OWNER_ROLE>`. The migration runner **refuses** to
apply files that still contain unresolved angle-bracket placeholders.

`002` **creates the two runtime SP roles** with `databricks_create_role()` and
grants each to `authenticator` (the only way the HTTP Data API can `SET ROLE` to
them — see "Data API role mapping"). You do **not** create these roles via the
SDK or the Roles UI. The service principals themselves must already exist in the
workspace (create with `databricks service-principals create`).

Apply in this order — **`002` is applied after `003`** because it grants
`EXECUTE` on the RPCs defined in `003` (the file guards against the wrong order):

```bash
python scripts/apply_sql.py --file sql/001_schema.sql        --apply   # schema, trigger, RLS
python scripts/apply_sql.py --file sql/003_rpc_functions.sql --apply   # SECURITY DEFINER RPCs
python scripts/apply_sql.py --file sql/002_roles_and_grants.sql --apply # roles + least-privilege grants
python scripts/apply_sql.py --file sql/004_cdf_prereqs.sql   --apply   # REPLICA IDENTITY FULL
```

(Drop `--apply` for a dry-run that validates placeholders and counts statements.)

## 3. Provision identity, Data API, and CDF configuration

Dry-run first, then `--apply` only after review:

```bash
python scripts/provision_lakebase.py \
  --project-id "$LAKEBASE_PROJECT_ID" --branch-id "$LAKEBASE_BRANCH_ID" \
  --database-id "$LAKEBASE_DATABASE_ID" \
  --cdf-catalog "$DATABRICKS_CDF_CATALOG" --cdf-schema "$DATABRICKS_CDF_SCHEMA" \
  --profile "$DATABRICKS_CONFIG_PROFILE"
```

Note `--database-id` is the Lakebase **database resource id** (e.g.
`db-xxxxxxxx`), which differs from the Postgres database name — list it with
`databricks postgres list-databases <branch>` or `w.postgres.list_databases`.

The script enables the Data API for the `claims` schema and configures CDF. It
does **not** create SP roles (those are created by `002` — see above). CDF
configuration is created once and is immutable; if an equivalent config exists,
the script reports it rather than creating a second one. If the workspace does
not expose the CDF API, configure
CDF in the Lakebase UI and keep notebook 02 unchanged.

## 4. Seed and run the demo

Run `notebooks/01_seed_synthetic_claims.py`. It creates two synthetic claims in
different business units and registers the two logical agents, then seeds initial
memory and one task per claim through the RPCs (so audit events exist immediately).

```bash
export DATA_API_URL='<Data API base URL from the Data API status output>'
export DBX_OAUTH_TOKEN='<short-lived token from the approved UiPath token flow>'
export CLAIM_ID='CLM-10001'
export WORKFLOW_ID='WF-10001'
./scripts/test_data_api.sh
```

The script performs a UiPath-style read, a memory write, task create/complete, and
negative tests for direct table access and cross-claim access. Do not put a client
secret or long-lived token in shell history or the source tree.

## 5. Verify in Unity Catalog

Run `notebooks/02_verify_agent_audit_and_cdf.py` with `CDF_CATALOG`,
`CDF_SCHEMA = "suncorp_claims_cdf"`, and `CLAIM_ID = "CLM-10001"`. The final output
shows application audit events for reads and writes; CDF history rows for
inserted/updated/deleted rows; request-id / trace-id correlation; the authenticated
principal and logical agent id; and an explicit note that **CDF is write history,
not read telemetry**.

## Data API role mapping (important, verified)

The Data API's PostgREST connects as the `authenticator` role and `SET ROLE`s to
the caller's Postgres role (the OAuth JWT `.sub`, per `jwt_role_claim_key`). For
that switch to be permitted, `authenticator` must hold **membership** in the
caller's role — and this only works when the SP role was created with
**`databricks_create_role()`**:

```sql
CREATE EXTENSION IF NOT EXISTS databricks_auth;
SELECT databricks_create_role('<sp-application-id>', 'SERVICE_PRINCIPAL');
GRANT "<sp-application-id>" TO authenticator;   -- succeeds only for the above
```

A role created via the SDK/CLI role API or the Roles UI **cannot** be granted to
`authenticator` (the `GRANT` is denied), and Data API calls as that SP then fail
with `42501 permission denied to set role "<sp>"` even though the role, its
privileges, and the JWT are all correct. `sql/002_roles_and_grants.sql` does this
the correct way for both runtime SPs. (A direct Postgres connection — psql/psycopg
as `user=<sp-application-id>` with the OAuth JWT as password — works regardless
and is a useful fallback for validating the RPC surface.)

This flow is verified end-to-end: as the provisioned SP over the HTTP Data API,
`rpc_read_memory` / `rpc_write_memory` / task create+complete return `200`, while
a cross-claim completion and a direct table read both return `403`.

## CDF target catalog storage (important, verified)

CDF materializes the change feed as managed Delta tables in the target Unity
Catalog catalog, so that catalog must have **working managed storage**. On a
serverless workspace, a catalog created on the metastore **Default Storage** can
fail every read/write with `403 ... credentialName = None` (and catalog creation
with an explicit storage root is UI-gated: *"Please use the UI to create a
catalog with Default Storage"*). Point `DATABRICKS_CDF_CATALOG` at a catalog
backed by a real **external location** instead — either the workspace's own
default catalog, or a new catalog created on the workspace external location:

```bash
databricks catalogs create <cdf_catalog> \
  --storage-root s3://<workspace-external-location-bucket>/<path> -p <profile>
```

CDF config is immutable, but you can `delete_cdf_config` + `create_cdf_config`
to repoint it at a storage-working catalog; CDF then materializes (and backfills
accumulated changes) into the new schema, and the history tables become
queryable. Verified end-to-end: agent-memory and task change history land in
`<cdf_catalog>.suncorp_claims_cdf` and are readable from SQL.

## Design notes

- Use separate service principals for UiPath and Databricks agent runtimes where
  strong runtime attribution is required.
- A logical `agent_id` supplied by a caller is not independently trustworthy if
  every agent shares one principal. The package records both the authenticated
  principal (`session_user`) and the logical agent id, and supports separate principals.
- Keep prompts, documents, and large tool results out of the audit table. Store
  references or hashes instead.
- Use the RPCs for multi-table operations so business mutation and audit insertion
  happen in one database transaction.
- The RPCs use explicit claim / business-unit checks because SECURITY DEFINER
  functions must not rely on caller-visible RLS alone. RLS is enabled as a
  fail-closed baseline (no policy for the runtime role ⇒ direct access denied),
  and is deliberately **not** forced on the owner so the definer functions work.
- Set `REPLICA IDENTITY FULL` on CDF-participating tables when before/after row
  images are required.

## Genie Code workflow

1. `genie_code_prompts/01_scaffold_review.txt` — inspect the package without executing state-changing statements.
2. Replace placeholders and run the offline tests (`python tests/test_sql_static.py`).
3. Compare the generated SDK signatures with the installed SDK version.
4. Execute SQL only after human review and approval.
5. `genie_code_prompts/02_acceptance_review.txt` — validate the end-to-end demo.

## Offline checks

```bash
python tests/test_sql_static.py     # 13 static safety + contract checks, no DB
```

## Sources used for interface choices

- Databricks SDK Postgres API: `CdfConfig`, `create_cdf_config`, `DataApi`,
  `DataApiDataApiSpec`, `create_data_api` (Autoscaling tier; newer SDK).
- Lakebase Data API connectivity: OAuth bearer token, matching Postgres identity,
  PostgREST RPC/CRUD, and RLS.
- Lakebase role guidance: managed identity role with `SERVICE_PRINCIPAL` and `LAKEBASE_OAUTH_V1`.
- Databricks CLI Autoscaling Postgres commands (`databricks postgres ...`) for
  endpoint host lookup and short-lived credential minting.
