# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Seed synthetic Suncorp claims, agents, memory, and tasks
# MAGIC
# MAGIC Seeds a small, **synthetic** claims-center dataset for the Lakebase
# MAGIC agent-memory demo:
# MAGIC
# MAGIC * Two claims in **different business units** (so the cross-claim boundary
# MAGIC   is meaningful).
# MAGIC * Two logical agents: `uipath.document_review.v1` and
# MAGIC   `databricks.claim_triage.v1`.
# MAGIC * Initial claim-scoped memory and one open task per claim, written through
# MAGIC   the approved RPCs so each seed operation also produces an audit event.
# MAGIC
# MAGIC Prerequisites: SQL migrations 001–004 already applied. Run as a reviewed
# MAGIC Python task or in a notebook. No credentials are stored here.

# COMMAND ----------
# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.133" --quiet

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
# MAGIC %md ## Configuration

# COMMAND ----------
import os

# Endpoint resource path: projects/<project>/branches/<branch>/endpoints/<endpoint>
LAKEBASE_ENDPOINT = os.environ.get("LAKEBASE_ENDPOINT", "<LAKEBASE_ENDPOINT>")
LAKEBASE_DB_NAME  = os.environ.get("LAKEBASE_DB_NAME", "claims_center")
PG_SCHEMA         = os.environ.get("LAKEBASE_PG_SCHEMA", "claims")

# The two synthetic claims, deliberately in different business units.
CLAIMS = [
    {"claim_id": "CLM-10001", "business_unit": "motor", "status": "open"},
    {"claim_id": "CLM-10002", "business_unit": "home",  "status": "open"},
]
AGENTS = [
    {"agent_id": "uipath.document_review.v1", "runtime": "uipath",
     "description": "UiPath document review agent"},
    {"agent_id": "databricks.claim_triage.v1", "runtime": "databricks",
     "description": "Databricks claim triage agent"},
]

# COMMAND ----------
# MAGIC %md
# MAGIC ## Connect to Lakebase
# MAGIC Uses a short-lived Lakebase OAuth database credential from the Databricks
# MAGIC SDK. The credential is not persisted.

# COMMAND ----------
import os
import psycopg
from databricks.sdk import WorkspaceClient

def lakebase_connection():
    """Open a psycopg connection to Lakebase Autoscaling via the Databricks SDK.

    Uses the SDK's Postgres API (WorkspaceClient().postgres) to resolve the
    endpoint host and mint a short-lived OAuth database credential — no CLI
    dependency, so it runs inside a Databricks notebook or job. Requires
    databricks-sdk >= 0.133 (installed in the %pip cell above). Verified on the
    field-eng workspace as a serverless job.
    """
    w = WorkspaceClient()
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT).token
    host = os.environ.get("LAKEBASE_HOST")
    if not host:
        host = w.postgres.get_endpoint(LAKEBASE_ENDPOINT).status.hosts.host
    return psycopg.connect(
        f"host={host} port=5432 dbname={LAKEBASE_DB_NAME} user={user} sslmode=require",
        password=token, autocommit=False,
    )

# COMMAND ----------
# MAGIC %md
# MAGIC ## Seed reference data (claims + agents)
# MAGIC Reference rows are inserted directly by this admin/seed task. Idempotent.

# COMMAND ----------
def seed_reference(conn):
    with conn.cursor() as cur:
        for c in CLAIMS:
            cur.execute(
                f"INSERT INTO {PG_SCHEMA}.claim (claim_id, business_unit, status) "
                "VALUES (%s, %s, %s) ON CONFLICT (claim_id) DO NOTHING",
                (c["claim_id"], c["business_unit"], c["status"]),
            )
        for a in AGENTS:
            cur.execute(
                f"INSERT INTO {PG_SCHEMA}.agent (agent_id, runtime, description) "
                "VALUES (%s, %s, %s) ON CONFLICT (agent_id) DO NOTHING",
                (a["agent_id"], a["runtime"], a["description"]),
            )
    conn.commit()
    print(f"Seeded {len(CLAIMS)} claims and {len(AGENTS)} agents.")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Seed memory + a task per claim, THROUGH the RPCs
# MAGIC Writing via the RPCs means each seed op also produces an append-only
# MAGIC audit event, so notebook 02 has data to show immediately.

# COMMAND ----------
def seed_agent_state(conn):
    with conn.cursor() as cur:
        # CLM-10001 (motor) -- UiPath document review agent
        cur.execute(
            f"SELECT {PG_SCHEMA}.rpc_write_memory(%s,%s,%s,%s,%s,%s)",
            ("CLM-10001", "uipath.document_review.v1", "triage.summary",
             '{"seeded": true, "priority": "standard"}', "seed-req", "seed-trace"),
        )
        cur.execute(
            f"SELECT {PG_SCHEMA}.rpc_create_task(%s,%s,%s,%s,%s,%s,%s)",
            ("CLM-10001", "WF-10001", "uipath.document_review.v1",
             "review_documents", '{"docs": 3}', "seed-req", "seed-trace"),
        )
        # CLM-10002 (home) -- Databricks triage agent
        cur.execute(
            f"SELECT {PG_SCHEMA}.rpc_write_memory(%s,%s,%s,%s,%s,%s)",
            ("CLM-10002", "databricks.claim_triage.v1", "triage.summary",
             '{"seeded": true, "priority": "expedited"}', "seed-req", "seed-trace"),
        )
        cur.execute(
            f"SELECT {PG_SCHEMA}.rpc_create_task(%s,%s,%s,%s,%s,%s,%s)",
            ("CLM-10002", "WF-10002", "databricks.claim_triage.v1",
             "triage_claim", '{}', "seed-req", "seed-trace"),
        )
    conn.commit()
    print("Seeded initial memory + one task per claim via RPCs.")

# COMMAND ----------
# MAGIC %md ## Run

# COMMAND ----------
with lakebase_connection() as _conn:
    seed_reference(_conn)
    seed_agent_state(_conn)
    with _conn.cursor() as _cur:
        _cur.execute(f"SELECT claim_id, business_unit, status FROM {PG_SCHEMA}.claim ORDER BY claim_id")
        for row in _cur.fetchall():
            print("claim:", row)
        _cur.execute(f"SELECT count(*) FROM {PG_SCHEMA}.audit_event")
        print("audit_event rows:", _cur.fetchone()[0])
print("Seed complete.")
