-- =============================================================================
-- 001_schema.sql
-- Suncorp claims-center Lakebase agent-memory demo
--
-- Schema, tables, indexes, append-only audit trigger, and a row-level-security
-- baseline for claim-scoped agent memory shared between Databricks and UiPath
-- agents.
--
-- SAFETY
--   * Synthetic-data starter. Contains NO credentials and NO destructive SQL.
--   * Assumes an administrative / project-owner connection.
--   * `CREATE ... IF NOT EXISTS` throughout so the file is re-runnable.
--   * The runtime principal is granted USAGE + EXECUTE on approved RPCs only
--     (see 002_roles_and_grants.sql). It never receives direct DML on these
--     tables.
--
-- The schema name is fixed to `claims` here to keep the demo self-consistent.
-- If you set LAKEBASE_PG_SCHEMA to something else, update it in one place below.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS claims;

SET search_path TO claims, public;

-- -----------------------------------------------------------------------------
-- Reference: claims and their owning business unit.
-- The business_unit column is the boundary the RPCs enforce cross-claim
-- isolation against.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims.claim (
    claim_id        TEXT PRIMARY KEY,
    business_unit   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Logical agents. `agent_id` is a caller-supplied logical identifier such as
-- `uipath.document_review.v1` or `databricks.claim_triage.v1`.
--
-- IMPORTANT: a logical agent_id is NOT independently trustworthy when several
-- agents share one authenticated principal. Audit events therefore record BOTH
-- the authenticated principal (current_user) and this logical agent_id.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims.agent (
    agent_id        TEXT PRIMARY KEY,
    runtime         TEXT NOT NULL,          -- 'uipath' | 'databricks'
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Claim-scoped agent memory. One row per (claim, memory_key). memory_value is
-- JSONB so agents can share structured working state.
--
-- Keep prompts, raw documents, and large tool results OUT of this table. Store
-- references or hashes instead.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims.agent_memory (
    memory_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id        TEXT NOT NULL REFERENCES claims.claim (claim_id),
    memory_key      TEXT NOT NULL,
    memory_value    JSONB NOT NULL,
    created_by      TEXT NOT NULL,          -- logical agent_id that first wrote it
    updated_by      TEXT NOT NULL,          -- logical agent_id of last writer
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, memory_key)
);

CREATE INDEX IF NOT EXISTS ix_agent_memory_claim
    ON claims.agent_memory (claim_id);

-- -----------------------------------------------------------------------------
-- Workflow tasks. A task belongs to exactly one claim + workflow and is owned
-- by a logical agent. Cross-claim task access is denied by the RPC layer.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims.task (
    task_id         TEXT PRIMARY KEY,
    claim_id        TEXT NOT NULL REFERENCES claims.claim (claim_id),
    workflow_id     TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'completed', 'cancelled')),
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_task_claim_workflow
    ON claims.task (claim_id, workflow_id);

-- -----------------------------------------------------------------------------
-- Append-only application audit. ONE row per logical agent operation.
--
--   principal      the authenticated database identity (SESSION_USER)
--   agent_id       the caller-supplied logical agent identifier
--   action         READ_MEMORY | WRITE_MEMORY | CREATE_TASK | COMPLETE_TASK ...
--   object_type    'agent_memory' | 'task'
--   request_id     supplied by the runtime for correlation
--   trace_id       supplied by the runtime for correlation
--
-- This is application audit (including READS). It is deliberately distinct from
-- Lakebase CDF, which captures ROW-CHANGE (write) history only.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims.audit_event (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id        TEXT NOT NULL,
    workflow_id     TEXT,
    principal       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    action          TEXT NOT NULL,
    object_type     TEXT NOT NULL,
    object_ref      TEXT,
    request_id      TEXT,
    trace_id        TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_event_claim
    ON claims.audit_event (claim_id, created_at);

-- -----------------------------------------------------------------------------
-- Append-only enforcement: reject UPDATE and DELETE on the audit table.
-- INSERT stays allowed (through the RPCs, which run as the definer).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claims.audit_event_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'claims.audit_event is append-only: % is not permitted',
        TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_event_append_only ON claims.audit_event;
CREATE TRIGGER trg_audit_event_append_only
    BEFORE UPDATE OR DELETE ON claims.audit_event
    FOR EACH ROW EXECUTE FUNCTION claims.audit_event_append_only();

-- -----------------------------------------------------------------------------
-- Row-level-security baseline.
--
-- RLS is enabled here as a defense-in-depth baseline. The demo does NOT rely on
-- caller-visible RLS for its claim isolation: the SECURITY DEFINER RPCs perform
-- explicit claim / business-unit checks (see 003_rpc_functions.sql), because a
-- security-definer function must not depend on the caller's RLS context.
--
-- With RLS enabled and no permissive policy for the runtime role, any direct
-- table access by that role returns zero rows (fail-closed). The table owner
-- (and therefore the SECURITY DEFINER RPCs, which run with the owner's rights)
-- bypasses RLS, so the approved code path keeps working.
--
-- NOTE: RLS is deliberately NOT forced on the owner. Forcing RLS would also
-- subject the definer functions to these policies and break the approved code
-- path, and the demo's real isolation guarantee lives in the explicit claim /
-- business-unit checks inside those functions, not in caller-visible RLS.
-- -----------------------------------------------------------------------------
ALTER TABLE claims.agent_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE claims.task         ENABLE ROW LEVEL SECURITY;
ALTER TABLE claims.audit_event  ENABLE ROW LEVEL SECURITY;

-- No permissive policy is created for the runtime principal on purpose: with
-- RLS enabled and zero policies, direct table reads/writes by a non-owner role
-- match no policy and are denied. The negative test in scripts/test_data_api.sh
-- relies on this to prove direct table access is blocked.
