#!/usr/bin/env bash
# =============================================================================
# test_data_api.sh -- UiPath-style Data API demo + negative tests.
#
# Exercises the approved RPC surface through the Lakebase Data API exactly as a
# UiPath agent would: authenticate with a Databricks OAuth bearer token, call
# PostgREST RPC endpoints, and confirm the security boundaries hold.
#
# Sequence:
#   1. READ  claim-scoped memory        (rpc_read_memory)
#   2. WRITE claim-scoped memory        (rpc_write_memory)
#   3. CREATE a workflow task           (rpc_create_task)
#   4. COMPLETE that task               (rpc_complete_task)
#   5. NEGATIVE: direct table access is denied
#   6. NEGATIVE: cross-claim task completion is denied
#
# SAFETY
#   * Never put a client secret or long-lived token in shell history or source.
#     Supply DBX_OAUTH_TOKEN from the approved short-lived UiPath token flow.
#
# Required environment:
#   DATA_API_URL   PostgREST base for the target database, ending in
#                  .../rest/<database>  (from the Data API status `url` field).
#                  Paths are "${DATA_API_URL}/<schema>/rpc/<fn>" for RPCs and
#                  "${DATA_API_URL}/<schema>/<table>" for table access.
#   DBX_OAUTH_TOKEN  short-lived Databricks OAuth bearer token (UiPath flow)
#   CLAIM_ID         e.g. CLM-10001
#   WORKFLOW_ID      e.g. WF-10001
# Optional:
#   AGENT_ID         logical agent id (default: uipath.document_review.v1)
#   CROSS_CLAIM_ID   a DIFFERENT claim id for the cross-claim test (default CLM-10002)
# =============================================================================
set -euo pipefail

: "${DATA_API_URL:?set DATA_API_URL}"
: "${DBX_OAUTH_TOKEN:?set DBX_OAUTH_TOKEN}"
: "${CLAIM_ID:?set CLAIM_ID}"
: "${WORKFLOW_ID:?set WORKFLOW_ID}"
AGENT_ID="${AGENT_ID:-uipath.document_review.v1}"
CROSS_CLAIM_ID="${CROSS_CLAIM_ID:-CLM-10002}"
# The Lakebase Data API puts the Postgres schema in the URL path:
#   ${DATA_API_URL}/<schema>/rpc/<function>   for RPC calls
#   ${DATA_API_URL}/<schema>/<table>          for table (CRUD) access
DATA_API_SCHEMA="${DATA_API_SCHEMA:-claims}"

AUTH=(-H "Authorization: Bearer ${DBX_OAUTH_TOKEN}")
JSON=(-H "Content-Type: application/json")
REQ_ID="req-$(date +%s)-$$"
TRACE_ID="trace-$(date +%s)-$$"

rpc() { # rpc <function> <json-body>
  curl -sS -X POST "${AUTH[@]}" "${JSON[@]}" \
    -d "$2" "${DATA_API_URL}/${DATA_API_SCHEMA}/rpc/$1"
}

echo "== 1. READ memory (rpc_read_memory) =="
rpc rpc_read_memory "$(cat <<JSON
{"p_claim_id":"${CLAIM_ID}","p_agent_id":"${AGENT_ID}",
 "p_memory_key":"triage.summary","p_request_id":"${REQ_ID}","p_trace_id":"${TRACE_ID}"}
JSON
)"
echo; echo

echo "== 2. WRITE memory (rpc_write_memory) =="
rpc rpc_write_memory "$(cat <<JSON
{"p_claim_id":"${CLAIM_ID}","p_agent_id":"${AGENT_ID}",
 "p_memory_key":"document_review.status",
 "p_memory_value":{"state":"reviewed","docs":3,"by":"${AGENT_ID}"},
 "p_request_id":"${REQ_ID}","p_trace_id":"${TRACE_ID}"}
JSON
)"
echo; echo

echo "== 3. CREATE task (rpc_create_task) =="
TASK_JSON="$(rpc rpc_create_task "$(cat <<JSON
{"p_claim_id":"${CLAIM_ID}","p_workflow_id":"${WORKFLOW_ID}","p_agent_id":"${AGENT_ID}",
 "p_task_type":"handoff_to_triage",
 "p_payload":{"note":"documents reviewed, ready for triage"},
 "p_request_id":"${REQ_ID}","p_trace_id":"${TRACE_ID}"}
JSON
)")"
echo "${TASK_JSON}"
# PostgREST returns a scalar function result as a JSON string/array; extract it.
TASK_ID="$(printf '%s' "${TASK_JSON}" | tr -d '[]"' | head -c 200)"
echo "created task_id=${TASK_ID}"
echo

echo "== 4. COMPLETE task (rpc_complete_task, same claim -> allowed) =="
rpc rpc_complete_task "$(cat <<JSON
{"p_task_id":"${TASK_ID}","p_claim_id":"${CLAIM_ID}","p_agent_id":"${AGENT_ID}",
 "p_request_id":"${REQ_ID}","p_trace_id":"${TRACE_ID}"}
JSON
)"
echo; echo

echo "== 5. NEGATIVE: direct table access must be DENIED =="
# The runtime principal has no table privileges and RLS has no permissive policy
# for it, so a direct GET on the memory table must NOT return claim rows.
STATUS="$(curl -sS -o /tmp/direct_table_body.txt -w '%{http_code}' \
  "${AUTH[@]}" "${DATA_API_URL}/${DATA_API_SCHEMA}/agent_memory?limit=1" || true)"
echo "HTTP ${STATUS}; body:"; cat /tmp/direct_table_body.txt; echo
if [ "${STATUS}" = "200" ] && grep -q 'memory_value' /tmp/direct_table_body.txt; then
  echo "FAIL: direct table access returned data -- boundary NOT enforced!"; exit 1
else
  echo "PASS: direct table access is denied (no rows returned)."
fi
echo

echo "== 6. NEGATIVE: cross-claim task completion must be DENIED =="
# The task created above belongs to CLAIM_ID. Asserting authority for a
# DIFFERENT claim (CROSS_CLAIM_ID) must be rejected by the RPC's cross-claim guard.
XBODY="$(rpc rpc_complete_task "$(cat <<JSON
{"p_task_id":"${TASK_ID}","p_claim_id":"${CROSS_CLAIM_ID}","p_agent_id":"${AGENT_ID}",
 "p_request_id":"${REQ_ID}","p_trace_id":"${TRACE_ID}"}
JSON
)" || true)"
echo "${XBODY}"
if printf '%s' "${XBODY}" | grep -qi 'cross-claim access denied'; then
  echo "PASS: cross-claim task completion was denied."
else
  echo "FAIL: cross-claim completion was NOT denied as expected!"; exit 1
fi
echo
echo "All Data API demo steps and negative tests completed."
