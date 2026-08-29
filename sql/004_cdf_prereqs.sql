-- =============================================================================
-- 004_cdf_prereqs.sql
-- Suncorp claims-center Lakebase agent-memory demo
--
-- Change Data Feed (CDF) prerequisites and verification.
--
-- Lakebase CDF surfaces Postgres ROW CHANGES for a schema as Unity Catalog
-- Delta history tables. To capture full before/after row images (not just the
-- primary key on UPDATE/DELETE), the participating tables must be set to
-- REPLICA IDENTITY FULL.
--
-- WHAT CDF IS / IS NOT (important for the demo narrative):
--   * CDF is WRITE history: inserts, updates, deletes of rows.
--   * CDF is NOT read telemetry. A READ of agent memory produces an
--     application audit_event row (see 003_rpc_functions.sql) but NO CDF row,
--     because no row changed. notebooks/02 makes this distinction explicit.
--
-- This file only sets the Postgres-side prerequisite. Enabling CDF itself
-- (the schema -> Unity Catalog history binding) is done by
-- scripts/provision_lakebase.py or the Lakebase UI, and is immutable once
-- created.
-- =============================================================================

SET search_path TO claims, public;

-- -----------------------------------------------------------------------------
-- REPLICA IDENTITY FULL on the tables whose full row images we want in CDF.
-- audit_event is append-only (inserts only) but is set too so its inserted row
-- image is complete and consistent with the others.
-- -----------------------------------------------------------------------------
ALTER TABLE claims.agent_memory REPLICA IDENTITY FULL;
ALTER TABLE claims.task         REPLICA IDENTITY FULL;
ALTER TABLE claims.audit_event  REPLICA IDENTITY FULL;

-- Reference tables also participate so the seeded claims/agents appear in CDF.
ALTER TABLE claims.claim        REPLICA IDENTITY FULL;
ALTER TABLE claims.agent        REPLICA IDENTITY FULL;

-- -----------------------------------------------------------------------------
-- VERIFICATION QUERIES (read-only; safe to run any time).
-- Run these after applying to confirm the prerequisite is in place.
-- Expected relreplident for every row below: 'f' (FULL).
--   relreplident codes: d = default, n = nothing, f = full, i = index
-- -----------------------------------------------------------------------------
-- SELECT n.nspname   AS schema_name,
--        c.relname   AS table_name,
--        c.relreplident AS replica_identity   -- expect 'f'
-- FROM   pg_class c
-- JOIN   pg_namespace n ON n.oid = c.relnamespace
-- WHERE  n.nspname = 'claims'
--   AND  c.relkind = 'r'
-- ORDER  BY c.relname;

-- Confirm RLS is enabled (baseline) on the memory/task/audit tables:
-- SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity
-- FROM   pg_class c
-- JOIN   pg_namespace n ON n.oid = c.relnamespace
-- WHERE  n.nspname = 'claims'
--   AND  c.relname IN ('agent_memory', 'task', 'audit_event')
-- ORDER  BY c.relname;

-- Confirm the append-only trigger exists on audit_event:
-- SELECT tgname, tgenabled
-- FROM   pg_trigger
-- WHERE  tgrelid = 'claims.audit_event'::regclass
--   AND  NOT tgisinternal;
