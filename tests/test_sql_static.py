#!/usr/bin/env python3
"""Offline static safety + contract checks for the demo SQL.

No database connection. Reads the sql/ files and asserts the safety properties
and the RPC/grant contract the rest of the package relies on. Runs under pytest
(`pytest tests/`) or standalone (`python tests/test_sql_static.py`).
"""
from __future__ import annotations

import os
import re

SQL_DIR = os.path.join(os.path.dirname(__file__), "..", "sql")


def _read(name: str) -> str:
    with open(os.path.join(SQL_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


SCHEMA = _read("001_schema.sql")
GRANTS = _read("002_roles_and_grants.sql")
RPCS = _read("003_rpc_functions.sql")
CDF = _read("004_cdf_prereqs.sql")
ALL_SQL = {"001": SCHEMA, "002": GRANTS, "003": RPCS, "004": CDF}

PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")


# --- Safety ------------------------------------------------------------------
def test_no_destructive_ddl():
    """No DROP TABLE/SCHEMA/DATABASE and no TRUNCATE anywhere."""
    banned = [r"\bDROP\s+TABLE\b", r"\bDROP\s+SCHEMA\b",
              r"\bDROP\s+DATABASE\b", r"\bTRUNCATE\b"]
    for name, text in ALL_SQL.items():
        up = text.upper()
        for pat in banned:
            assert not re.search(pat, up), f"{name}: destructive statement {pat}"


def test_no_unqualified_delete():
    """No DELETE without a WHERE clause (the audit trigger even blocks DELETE)."""
    for name, text in ALL_SQL.items():
        for m in re.finditer(r"\bDELETE\s+FROM\s+[^\;]+", text, re.IGNORECASE):
            frag = m.group(0)
            assert re.search(r"\bWHERE\b", frag, re.IGNORECASE), \
                f"{name}: unqualified DELETE: {frag[:60]}"


def test_no_hardcoded_credentials():
    """No PATs, passwords, or private keys embedded in the SQL."""
    for name, text in ALL_SQL.items():
        assert "dapi" not in text.lower(), f"{name}: looks like a PAT token"
        assert "PRIVATE KEY" not in text.upper(), f"{name}: embedded private key"
        assert not re.search(r"PASSWORD\s+'", text, re.IGNORECASE), \
            f"{name}: literal PASSWORD"


# --- Append-only audit -------------------------------------------------------
def test_audit_append_only_trigger():
    assert "audit_event_append_only" in SCHEMA
    assert re.search(r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+claims\.audit_event",
                     SCHEMA, re.IGNORECASE), "append-only trigger missing"


# --- RLS baseline ------------------------------------------------------------
def test_rls_enabled_on_sensitive_tables():
    for tbl in ("agent_memory", "task", "audit_event"):
        assert re.search(rf"ALTER TABLE claims\.{tbl}\s+ENABLE ROW LEVEL SECURITY",
                         SCHEMA), f"RLS not enabled on {tbl}"


def test_rls_not_forced():
    """RLS must NOT be forced, or the SECURITY DEFINER RPCs would break."""
    assert "FORCE ROW LEVEL SECURITY" not in SCHEMA.upper()


# --- RPC contract ------------------------------------------------------------
RPC_NAMES = ("rpc_read_memory", "rpc_write_memory",
             "rpc_create_task", "rpc_complete_task")


def test_rpcs_are_security_definer():
    for fn in RPC_NAMES:
        block = re.search(
            rf"CREATE OR REPLACE FUNCTION claims\.{fn}\b.*?\$\$;",
            RPCS, re.IGNORECASE | re.DOTALL,
        )
        assert block, f"{fn} not found"
        body = block.group(0)
        assert "SECURITY DEFINER" in body, f"{fn} is not SECURITY DEFINER"
        assert re.search(r"SET search_path\s*=\s*claims,\s*pg_temp", body), \
            f"{fn} does not pin search_path"


def test_principal_recorded_as_session_user():
    """Audit must record the authenticated identity, not the definer."""
    assert "session_user" in RPCS
    assert "current_user" not in RPCS.replace("current_user,", ""), \
        "audit should use session_user, not current_user, for the principal"


def test_cross_claim_guard_present():
    assert re.search(r"cross-claim access denied", RPCS, re.IGNORECASE), \
        "cross-claim guard message missing from rpc_complete_task"
    assert re.search(r"v_task_claim\s*<>\s*p_claim_id", RPCS), \
        "cross-claim comparison (v_task_claim <> p_claim_id) missing"


# --- Grants: least privilege -------------------------------------------------
def test_grants_execute_only_no_table_dml_to_runtime():
    # Runtime principals get EXECUTE on the RPCs (grants may list several
    # functions in one GRANT EXECUTE ON FUNCTION statement).
    assert "GRANT EXECUTE ON FUNCTION" in GRANTS, "no EXECUTE grant present"
    execute_grants = " ".join(
        m.group(0) for m in re.finditer(
            r"GRANT EXECUTE ON FUNCTION.*?;", GRANTS, re.DOTALL)
    )
    for fn in RPC_NAMES:
        assert f"claims.{fn}(" in execute_grants, f"missing EXECUTE grant for {fn}"
    # ...and must NOT receive INSERT/UPDATE/DELETE/SELECT on the tables.
    assert not re.search(
        r"GRANT\s+(SELECT|INSERT|UPDATE|DELETE)[^;]*ON\s+claims\.(agent_memory|task|audit_event)",
        GRANTS, re.IGNORECASE), "runtime role granted direct table DML"


def test_grant_signatures_match_rpc_definitions():
    """Every EXECUTE grant names a function that is actually defined."""
    for fn in RPC_NAMES:
        assert f"claims.{fn}(" in RPCS, f"{fn} referenced in grants but not defined"


def test_sp_roles_created_data_api_compatible():
    """SP roles must be created via databricks_create_role and granted to the
    Data API authenticator (raw SDK/UI-created roles are not grantable)."""
    assert "databricks_create_role" in GRANTS, \
        "002 must create SP roles with databricks_create_role()"
    assert re.search(r'GRANT\s+"?<UIPATH_SP_CLIENT_ID>"?\s+TO\s+authenticator',
                     GRANTS), "UiPath SP role not granted to authenticator"
    assert re.search(r'GRANT\s+"?<DATABRICKS_AGENT_SP_CLIENT_ID>"?\s+TO\s+authenticator',
                     GRANTS), "Databricks agent SP role not granted to authenticator"


# --- CDF prerequisites -------------------------------------------------------
def test_replica_identity_full():
    for tbl in ("agent_memory", "task", "audit_event"):
        assert re.search(rf"ALTER TABLE claims\.{tbl}\s+REPLICA IDENTITY FULL",
                         CDF), f"REPLICA IDENTITY FULL missing for {tbl}"


# --- Placeholder contract ----------------------------------------------------
def test_only_grants_file_has_placeholders():
    """001/003/004 must be directly appliable; 002 carries the placeholders."""
    assert PLACEHOLDER_RE.search(GRANTS), "002 should contain placeholders"
    for name in ("001", "003", "004"):
        found = PLACEHOLDER_RE.findall(ALL_SQL[name])
        assert not found, f"{name} contains unresolved placeholders: {found}"


def _run_standalone() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
