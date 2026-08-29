# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Verify agent audit + CDF, and build the demo timeline
# MAGIC
# MAGIC Produces the demo timeline for one claim and makes the key distinction
# MAGIC explicit:
# MAGIC
# MAGIC * **Application audit** (`claims.audit_event`) records EVERY logical agent
# MAGIC   operation — including **reads**.
# MAGIC * **Lakebase CDF** (Unity Catalog history tables) records **row changes
# MAGIC   only** — inserts / updates / deletes. A read produces an audit event but
# MAGIC   **no** CDF row.
# MAGIC
# MAGIC The final output shows, for the target claim:
# MAGIC * application audit events for both reads and writes;
# MAGIC * CDF history rows for inserted / updated / deleted rows;
# MAGIC * request-id / trace-id correlation;
# MAGIC * the authenticated principal and the logical agent id;
# MAGIC * an explicit note that CDF is write history, not read telemetry.

# COMMAND ----------
# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.133" --quiet

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
# MAGIC %md ## Configuration

# COMMAND ----------
# CDF_CATALOG must be a catalog with WORKING managed storage (an explicit
# external-location storage_root). On serverless workspaces, a catalog on the
# metastore Default Storage can fail reads/writes with a 403 credential error --
# see README "CDF target catalog storage". The sandbox build uses `suncorp_cdf`
# (created on the workspace external location); Suncorp swaps in their own.
CDF_CATALOG = "suncorp_cdf"                 # DATABRICKS_CDF_CATALOG
CDF_SCHEMA  = "suncorp_claims_cdf"          # DATABRICKS_CDF_SCHEMA
CLAIM_ID    = "CLM-10001"

# Postgres-side (application audit) connection config -- see notebook 01.
import os
LAKEBASE_ENDPOINT = os.environ.get("LAKEBASE_ENDPOINT", "<LAKEBASE_ENDPOINT>")
LAKEBASE_DB_NAME  = os.environ.get("LAKEBASE_DB_NAME", "claims_center")
PG_SCHEMA         = os.environ.get("LAKEBASE_PG_SCHEMA", "claims")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Part A — Application audit (includes READS)
# MAGIC Read directly from `claims.audit_event`. This is the source of truth for
# MAGIC *what an agent did*, reads included.

# COMMAND ----------
import os
import psycopg
from databricks.sdk import WorkspaceClient

def lakebase_connection():
    """psycopg connection to Lakebase Autoscaling via the Databricks SDK (no CLI).
    Requires databricks-sdk >= 0.133. Verified on the field-eng workspace."""
    w = WorkspaceClient()
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT).token
    host = os.environ.get("LAKEBASE_HOST")
    if not host:
        host = w.postgres.get_endpoint(LAKEBASE_ENDPOINT).status.hosts.host
    return psycopg.connect(
        f"host={host} port=5432 dbname={LAKEBASE_DB_NAME} user={user} sslmode=require",
        password=token,
    )

# COMMAND ----------
audit_rows = []
with lakebase_connection() as conn:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT created_at, action, object_type, object_ref,
                   principal, agent_id, request_id, trace_id
            FROM   {PG_SCHEMA}.audit_event
            WHERE  claim_id = %s
            ORDER  BY created_at, event_id
            """,
            (CLAIM_ID,),
        )
        cols = [d[0] for d in cur.description]
        audit_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

audit_df = spark.createDataFrame(audit_rows) if audit_rows else None
print(f"Application audit events for {CLAIM_ID}: {len(audit_rows)}")
if audit_df:
    display(audit_df)

# COMMAND ----------
# MAGIC %md
# MAGIC **Note the READ_MEMORY rows above.** These exist in application audit but
# MAGIC will have NO counterpart in CDF, because a read changes no row.

# COMMAND ----------
# MAGIC %md
# MAGIC ## Part B — Lakebase CDF (row-change / WRITE history)
# MAGIC CDF materializes Postgres row changes as Unity Catalog Delta history
# MAGIC tables under the configured catalog/schema. We read the change history
# MAGIC for the memory and task tables.

# COMMAND ----------
def cdf_history(table_name: str):
    fqn = f"{CDF_CATALOG}.{CDF_SCHEMA}.{table_name}"
    try:
        df = spark.read.table(fqn)
        # Filter to this claim where the column exists.
        if "claim_id" in df.columns:
            df = df.filter(df.claim_id == CLAIM_ID)
        return df
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read CDF history table {fqn}: {exc}")
        print("Confirm CDF is configured (scripts/provision_lakebase.py or UI) "
              "and that the history tables have materialized.")
        return None

# COMMAND ----------
print("CDF row-change history — agent_memory (writes only):")
mem_cdf = cdf_history("agent_memory")
if mem_cdf is not None:
    display(mem_cdf)

print("CDF row-change history — task (writes only):")
task_cdf = cdf_history("task")
if task_cdf is not None:
    display(task_cdf)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Part C — Reconcile: audit vs CDF
# MAGIC Shows the counts side by side and states the invariant explicitly.

# COMMAND ----------
read_events  = [r for r in audit_rows if r["action"] == "READ_MEMORY"]
write_events = [r for r in audit_rows if r["action"] in
                ("WRITE_MEMORY", "CREATE_TASK", "COMPLETE_TASK")]

print("=" * 68)
print(f"Claim: {CLAIM_ID}")
print(f"  Application audit — reads : {len(read_events)}")
print(f"  Application audit — writes: {len(write_events)}")
print("  CDF captures WRITES only (see Part B); reads never appear in CDF.")
print("-" * 68)
print("  request-id / trace-id / principal / logical-agent correlation:")
for r in audit_rows[:10]:
    print(f"    {r['created_at']} | {r['action']:>18} | "
          f"principal={r['principal']} | agent={r['agent_id']} | "
          f"req={r['request_id']} | trace={r['trace_id']}")
print("=" * 68)
print("INVARIANT: CDF is WRITE history, not read telemetry. Read attribution "
      "comes from claims.audit_event, not from CDF.")
