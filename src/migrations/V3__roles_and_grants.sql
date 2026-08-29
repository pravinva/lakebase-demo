-- =============================================================================
-- V3__roles_and_grants.sql
-- Runtime service-principal roles + least-privilege grants (Data-API compatible).
--
-- Below the declarative line: application authorization, applied after V2 (RPCs).
-- The tokens {{uipath_sp}} and {{dbx_agent_sp}} are the service principals'
-- application/client IDs; deploy_schema.py substitutes them from the job's
-- --var parameters (kept out of version control).
--
-- Roles are created with databricks_create_role() (not the SDK/UI) so they can
-- be granted to the Data API `authenticator` role. Objects are owned by the
-- deploying/migration identity; the runtime roles receive EXECUTE on the RPCs
-- only -- no direct table DML.
-- =============================================================================
SET search_path TO claims, public;

-- Guard: the RPCs must exist (V2 applied first).
DO $$
BEGIN
    IF to_regprocedure('claims.rpc_read_memory(text,text,text,text,text)') IS NULL THEN
        RAISE EXCEPTION 'Apply V2__rpcs.sql before V3__roles_and_grants.sql (RPCs must exist to be granted).';
    END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS databricks_auth;

-- Create the two managed SP roles the Data-API-compatible way (idempotent).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{{uipath_sp}}') THEN
        PERFORM databricks_create_role('{{uipath_sp}}', 'SERVICE_PRINCIPAL');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{{dbx_agent_sp}}') THEN
        PERFORM databricks_create_role('{{dbx_agent_sp}}', 'SERVICE_PRINCIPAL');
    END IF;
END $$;

-- The Data API's authenticator must be able to SET ROLE to each runtime role.
GRANT "{{uipath_sp}}"    TO authenticator;
GRANT "{{dbx_agent_sp}}" TO authenticator;

-- Schema USAGE only (no access to table rows).
GRANT USAGE ON SCHEMA claims TO "{{uipath_sp}}", "{{dbx_agent_sp}}";
REVOKE ALL ON claims.claim, claims.agent, claims.agent_memory, claims.task,
    claims.audit_event FROM "{{uipath_sp}}", "{{dbx_agent_sp}}";

-- Lock down PUBLIC execute; grant EXECUTE only to the runtime roles.
REVOKE ALL ON FUNCTION
    claims._audit(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    claims.rpc_read_memory(TEXT, TEXT, TEXT, TEXT, TEXT),
    claims.rpc_write_memory(TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_create_task(TEXT, TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_complete_task(TEXT, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    claims.rpc_read_memory(TEXT, TEXT, TEXT, TEXT, TEXT),
    claims.rpc_write_memory(TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_create_task(TEXT, TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_complete_task(TEXT, TEXT, TEXT, TEXT, TEXT)
    TO "{{uipath_sp}}", "{{dbx_agent_sp}}";

-- Keep the least-privilege posture sticky for future objects.
ALTER DEFAULT PRIVILEGES IN SCHEMA claims
    REVOKE ALL ON TABLES FROM "{{uipath_sp}}", "{{dbx_agent_sp}}";
ALTER DEFAULT PRIVILEGES IN SCHEMA claims
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
