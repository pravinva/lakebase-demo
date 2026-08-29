-- =============================================================================
-- 002_roles_and_grants.sql
-- Suncorp claims-center Lakebase agent-memory demo
--
-- Creates the runtime service-principal Postgres roles the DATA-API-COMPATIBLE
-- way and grants them least privilege.
--
-- BEFORE APPLYING, replace these placeholders (the apply_sql.py runner REFUSES
-- to apply any file that still contains unresolved angle brackets):
--
--     <UIPATH_SP_CLIENT_ID>            UiPath service-principal APPLICATION /
--                                      client ID (a UUID). NOT the display name.
--     <DATABRICKS_AGENT_SP_CLIENT_ID>  Databricks agent service-principal
--                                      application / client ID (a UUID).
--     <CLAIMS_OWNER_ROLE>              Owner role that owns the schema and the
--                                      SECURITY DEFINER functions. If you keep
--                                      the objects owned by the admin/project
--                                      owner, set this to that role name.
--
-- WHY databricks_create_role (verified): the Lakebase Data API (PostgREST)
-- connects as the `authenticator` role and then `SET ROLE`s to the caller's
-- Postgres role (the JWT `.sub`). For that switch to be permitted, `authenticator`
-- must hold membership in the caller's role. A role created with the
-- databricks_create_role() helper CAN be granted to `authenticator`; a role
-- created via the SDK/CLI role API or the Roles UI CANNOT (the manual GRANT is
-- denied), and Data API calls then fail with
--   42501 permission denied to set role "<sp>".
-- So we create the SP roles here with databricks_create_role() and grant them to
-- `authenticator`. (Direct Postgres connections work either way; this matters
-- specifically for the HTTP Data API path.)
--
-- Least-privilege contract for each runtime role:
--   * membership in `authenticator` (so the Data API can SET ROLE to it)
--   * USAGE on the claims schema (to resolve the RPC names)
--   * EXECUTE on the approved RPC functions ONLY
--   * NO table-level SELECT/INSERT/UPDATE/DELETE on memory, task, or audit
--
-- APPLY ORDER: this file grants EXECUTE on the RPCs, so it must be applied
-- AFTER 003_rpc_functions.sql. Documented order:
--   001_schema.sql -> 003_rpc_functions.sql -> 002_roles_and_grants.sql -> 004_cdf_prereqs.sql
-- The guard below fails fast with a clear message if 003 has not run yet.
--
-- OWNERSHIP: set <CLAIMS_OWNER_ROLE> to the role that OWNS the tables and
-- functions and APPLIES these migrations. SECURITY DEFINER functions bypass RLS
-- only for their owner, so the function owner and table owner must be the same
-- role. The simplest correct choice is your admin/project-owner login.
--
-- Re-runnable: role creation is guarded by existence checks; GRANTs are idempotent.
-- =============================================================================

SET search_path TO claims, public;

-- Dependency guard: the RPCs must exist before we can grant EXECUTE on them.
DO $$
BEGIN
    IF to_regprocedure('claims.rpc_read_memory(text,text,text,text,text)') IS NULL THEN
        RAISE EXCEPTION
            'Apply sql/003_rpc_functions.sql BEFORE 002_roles_and_grants.sql '
            '(the RPCs must exist to be granted EXECUTE).';
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Object ownership (see header). No-op if the objects are already owned by the
-- applying admin and <CLAIMS_OWNER_ROLE> is that same login.
-- -----------------------------------------------------------------------------
ALTER SCHEMA claims OWNER TO "<CLAIMS_OWNER_ROLE>";
ALTER TABLE claims.claim         OWNER TO "<CLAIMS_OWNER_ROLE>";
ALTER TABLE claims.agent         OWNER TO "<CLAIMS_OWNER_ROLE>";
ALTER TABLE claims.agent_memory  OWNER TO "<CLAIMS_OWNER_ROLE>";
ALTER TABLE claims.task          OWNER TO "<CLAIMS_OWNER_ROLE>";
ALTER TABLE claims.audit_event   OWNER TO "<CLAIMS_OWNER_ROLE>";

-- -----------------------------------------------------------------------------
-- Create the two managed SP roles the Data-API-compatible way.
-- databricks_create_role(<identity>, <identity_type>) creates the Postgres role
-- backed by the Databricks identity with LAKEBASE_OAUTH_V1 auth. The applying
-- session receives ADMIN on the new role, which is what lets the subsequent
-- GRANT ... TO authenticator succeed.
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS databricks_auth;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '<UIPATH_SP_CLIENT_ID>') THEN
        PERFORM databricks_create_role('<UIPATH_SP_CLIENT_ID>', 'SERVICE_PRINCIPAL');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '<DATABRICKS_AGENT_SP_CLIENT_ID>') THEN
        PERFORM databricks_create_role('<DATABRICKS_AGENT_SP_CLIENT_ID>', 'SERVICE_PRINCIPAL');
    END IF;
END $$;

-- Let the Data API's authenticator SET ROLE to each SP role. Idempotent.
GRANT "<UIPATH_SP_CLIENT_ID>"           TO authenticator;
GRANT "<DATABRICKS_AGENT_SP_CLIENT_ID>" TO authenticator;

-- -----------------------------------------------------------------------------
-- Runtime principals: schema USAGE only (no access to table rows).
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA claims TO "<UIPATH_SP_CLIENT_ID>";
GRANT USAGE ON SCHEMA claims TO "<DATABRICKS_AGENT_SP_CLIENT_ID>";

-- -----------------------------------------------------------------------------
-- Explicitly ensure NO direct table privileges (REVOKE is a no-op if never
-- granted; kept as an explicit, re-assertable contract).
-- -----------------------------------------------------------------------------
REVOKE ALL ON claims.claim, claims.agent, claims.agent_memory, claims.task,
    claims.audit_event FROM "<UIPATH_SP_CLIENT_ID>";
REVOKE ALL ON claims.claim, claims.agent, claims.agent_memory, claims.task,
    claims.audit_event FROM "<DATABRICKS_AGENT_SP_CLIENT_ID>";

-- -----------------------------------------------------------------------------
-- Lock down PUBLIC EXECUTE (Postgres grants it by default at create time), then
-- grant EXECUTE only to the two named runtime principals. The internal
-- claims._audit helper is revoked from PUBLIC and granted to no runtime role.
-- -----------------------------------------------------------------------------
REVOKE ALL ON FUNCTION
    claims._audit(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB)
    FROM PUBLIC;

REVOKE ALL ON FUNCTION
    claims.rpc_read_memory(TEXT, TEXT, TEXT, TEXT, TEXT),
    claims.rpc_write_memory(TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_create_task(TEXT, TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_complete_task(TEXT, TEXT, TEXT, TEXT, TEXT)
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
    claims.rpc_read_memory(TEXT, TEXT, TEXT, TEXT, TEXT),
    claims.rpc_write_memory(TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_create_task(TEXT, TEXT, TEXT, TEXT, JSONB, TEXT, TEXT),
    claims.rpc_complete_task(TEXT, TEXT, TEXT, TEXT, TEXT)
    TO "<UIPATH_SP_CLIENT_ID>", "<DATABRICKS_AGENT_SP_CLIENT_ID>";

-- -----------------------------------------------------------------------------
-- Make the least-privilege posture sticky for any future objects.
-- -----------------------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA claims
    REVOKE ALL ON TABLES FROM "<UIPATH_SP_CLIENT_ID>", "<DATABRICKS_AGENT_SP_CLIENT_ID>";
ALTER DEFAULT PRIVILEGES IN SCHEMA claims
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
