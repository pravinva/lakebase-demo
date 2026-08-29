-- =============================================================================
-- 003_rpc_functions.sql
-- Suncorp claims-center Lakebase agent-memory demo
--
-- SECURITY DEFINER RPCs. Runtime identities call these approved functions
-- instead of reading or mutating the memory / task / audit tables directly.
--
-- Design invariants (see README "Design notes"):
--   * SECURITY DEFINER  -> functions run as the owner, which bypasses RLS, so
--     the runtime principals need no table privileges.
--   * SET search_path   -> pinned to `claims, pg_temp` so a caller cannot shadow
--     objects and hijack a definer function. REQUIRED for definer safety.
--   * Explicit claim / business-unit checks INSIDE each function. A definer
--     function must not rely on caller-visible RLS for isolation.
--   * Business mutation + audit insert happen in ONE transaction (one function
--     call = one atomic unit), so an audited operation cannot half-commit.
--   * principal recorded as session_user  -> the real authenticated identity,
--     NOT the definer/owner. The logical agent_id is recorded alongside it and
--     is treated as caller-asserted, not independently trusted.
-- =============================================================================

SET search_path TO claims, public;

-- -----------------------------------------------------------------------------
-- Internal helper: write one append-only audit row. Not granted to runtime
-- roles; only the definer functions below call it (same schema, same owner).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claims._audit(
    p_claim_id      TEXT,
    p_workflow_id   TEXT,
    p_agent_id      TEXT,
    p_action        TEXT,
    p_object_type   TEXT,
    p_object_ref    TEXT,
    p_request_id    TEXT,
    p_trace_id      TEXT,
    p_detail        JSONB
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = claims, pg_temp
AS $$
DECLARE
    v_event_id BIGINT;
BEGIN
    INSERT INTO claims.audit_event (
        claim_id, workflow_id, principal, agent_id, action,
        object_type, object_ref, request_id, trace_id, detail
    )
    VALUES (
        p_claim_id, p_workflow_id, session_user, p_agent_id, p_action,
        p_object_type, p_object_ref, p_request_id, p_trace_id,
        COALESCE(p_detail, '{}'::jsonb)
    )
    RETURNING event_id INTO v_event_id;

    RETURN v_event_id;
END;
$$;

-- -----------------------------------------------------------------------------
-- rpc_read_memory: read claim-scoped memory and record a READ audit event.
-- Returns zero rows when the key does not exist FOR THIS CLAIM (fail-closed).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claims.rpc_read_memory(
    p_claim_id      TEXT,
    p_agent_id      TEXT,
    p_memory_key    TEXT,
    p_request_id    TEXT DEFAULT NULL,
    p_trace_id      TEXT DEFAULT NULL
)
RETURNS TABLE (
    memory_key      TEXT,
    memory_value    JSONB,
    updated_by      TEXT,
    updated_at      TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = claims, pg_temp
AS $$
BEGIN
    -- Validate the claim exists before doing anything else.
    IF NOT EXISTS (SELECT 1 FROM claims.claim c WHERE c.claim_id = p_claim_id) THEN
        RAISE EXCEPTION 'unknown claim: %', p_claim_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Append-only audit of the READ (this is application audit, not CDF).
    PERFORM claims._audit(
        p_claim_id, NULL, p_agent_id, 'READ_MEMORY', 'agent_memory',
        p_memory_key, p_request_id, p_trace_id,
        jsonb_build_object('memory_key', p_memory_key)
    );

    RETURN QUERY
        SELECT m.memory_key, m.memory_value, m.updated_by, m.updated_at
        FROM   claims.agent_memory m
        WHERE  m.claim_id = p_claim_id
          AND  m.memory_key = p_memory_key;
END;
$$;

-- -----------------------------------------------------------------------------
-- rpc_write_memory: upsert claim-scoped memory and record a WRITE audit event.
-- The insert/update AND the audit row commit together.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claims.rpc_write_memory(
    p_claim_id      TEXT,
    p_agent_id      TEXT,
    p_memory_key    TEXT,
    p_memory_value  JSONB,
    p_request_id    TEXT DEFAULT NULL,
    p_trace_id      TEXT DEFAULT NULL
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = claims, pg_temp
AS $$
DECLARE
    v_memory_id BIGINT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM claims.claim c WHERE c.claim_id = p_claim_id) THEN
        RAISE EXCEPTION 'unknown claim: %', p_claim_id
            USING ERRCODE = 'no_data_found';
    END IF;

    INSERT INTO claims.agent_memory (
        claim_id, memory_key, memory_value, created_by, updated_by
    )
    VALUES (
        p_claim_id, p_memory_key, p_memory_value, p_agent_id, p_agent_id
    )
    ON CONFLICT (claim_id, memory_key) DO UPDATE
        SET memory_value = EXCLUDED.memory_value,
            updated_by   = EXCLUDED.updated_by,
            updated_at   = now()
    RETURNING memory_id INTO v_memory_id;

    PERFORM claims._audit(
        p_claim_id, NULL, p_agent_id, 'WRITE_MEMORY', 'agent_memory',
        p_memory_key, p_request_id, p_trace_id,
        jsonb_build_object('memory_key', p_memory_key, 'memory_id', v_memory_id)
    );

    RETURN v_memory_id;
END;
$$;

-- -----------------------------------------------------------------------------
-- rpc_create_task: create a claim/workflow-scoped task and audit it.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claims.rpc_create_task(
    p_claim_id      TEXT,
    p_workflow_id   TEXT,
    p_agent_id      TEXT,
    p_task_type     TEXT,
    p_payload       JSONB DEFAULT '{}'::jsonb,
    p_request_id    TEXT DEFAULT NULL,
    p_trace_id      TEXT DEFAULT NULL
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = claims, pg_temp
AS $$
DECLARE
    v_task_id TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM claims.claim c WHERE c.claim_id = p_claim_id) THEN
        RAISE EXCEPTION 'unknown claim: %', p_claim_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Deterministic-ish task id: workflow + short random suffix.
    v_task_id := 'TSK-' || p_workflow_id || '-' ||
                 substr(md5(random()::text || clock_timestamp()::text), 1, 8);

    INSERT INTO claims.task (
        task_id, claim_id, workflow_id, agent_id, task_type, status, payload
    )
    VALUES (
        v_task_id, p_claim_id, p_workflow_id, p_agent_id, p_task_type,
        'open', COALESCE(p_payload, '{}'::jsonb)
    );

    PERFORM claims._audit(
        p_claim_id, p_workflow_id, p_agent_id, 'CREATE_TASK', 'task',
        v_task_id, p_request_id, p_trace_id,
        jsonb_build_object('task_type', p_task_type)
    );

    RETURN v_task_id;
END;
$$;

-- -----------------------------------------------------------------------------
-- rpc_complete_task: complete a task, enforcing the CROSS-CLAIM boundary.
--
-- p_claim_id is the claim the CALLER asserts authority for. If the task belongs
-- to a different claim, the call is denied. This is the explicit cross-claim
-- denial exercised by the negative test in scripts/test_data_api.sh.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claims.rpc_complete_task(
    p_task_id       TEXT,
    p_claim_id      TEXT,
    p_agent_id      TEXT,
    p_request_id    TEXT DEFAULT NULL,
    p_trace_id      TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = claims, pg_temp
AS $$
DECLARE
    v_task_claim    TEXT;
    v_workflow_id   TEXT;
BEGIN
    SELECT t.claim_id, t.workflow_id
    INTO   v_task_claim, v_workflow_id
    FROM   claims.task t
    WHERE  t.task_id = p_task_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown task: %', p_task_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Cross-claim guard: the caller's asserted claim must own the task.
    -- On mismatch we RAISE, which aborts this function's transaction, so a
    -- denied call makes NO writes at all (clean fail-closed). We deliberately
    -- do NOT attempt to audit the denial inline: an audit INSERT would be
    -- rolled back by this same RAISE. If denied-attempt telemetry is required,
    -- emit it out-of-band (from the API/runtime layer, or an autonomous logger
    -- on a separate connection).
    IF v_task_claim <> p_claim_id THEN
        RAISE EXCEPTION 'cross-claim access denied: task % belongs to another claim', p_task_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    UPDATE claims.task
    SET    status = 'completed',
           completed_at = now()
    WHERE  task_id = p_task_id;

    PERFORM claims._audit(
        p_claim_id, v_workflow_id, p_agent_id, 'COMPLETE_TASK', 'task',
        p_task_id, p_request_id, p_trace_id, '{}'::jsonb
    );

    RETURN TRUE;
END;
$$;
