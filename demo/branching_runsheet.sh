#!/usr/bin/env bash
# =============================================================================
# branching_runsheet.sh — live demo of Lakebase branching, two ways.
#
#   PART A  Manual (on stage): create a dev branch, connect with psql, show the
#           schema/RPCs/data were cloned, prove isolation, drop the branch.
#   PART B  Automated (GitHub Actions): a git push opens a PR and CI creates a
#           Lakebase branch on a self-hosted runner; closing the PR deletes it.
#
# Requires: databricks CLI (profile), jq, and psql (Part A only).
# Nothing is destructive to production. All verified live on 2026-08-29.
#
# Usage:
#   PROFILE=<cli-profile> demo/branching_runsheet.sh a      # Part A end-to-end
#   PROFILE=<cli-profile> demo/branching_runsheet.sh a-keep # Part A, keep the branch
#   demo/branching_runsheet.sh b                            # Part B instructions
# =============================================================================
set -euo pipefail

PROFILE="${PROFILE:-tko}"
P="projects/${LAKEBASE_PROJECT_ID:-suncorp-claims-center}"
SRC_BRANCH="${LAKEBASE_SOURCE_BRANCH:-production}"
DEV_BRANCH="${DEV_BRANCH:-dev}"
DB="${LAKEBASE_DB_NAME:-claims_center}"
REPO="${REPO:-pravinva/lakebase-demo}"
PSQL_BIN="${PSQL_BIN:-/opt/homebrew/opt/postgresql@15/bin/psql}"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

part_a() {
  local keep="${1:-no}"
  step "1. Create the dev branch off ${SRC_BRANCH} (copy-on-write, instant)"
  databricks postgres create-branch "$P" "$DEV_BRANCH" \
    --json "{\"spec\": {\"source_branch\": \"$P/branches/$SRC_BRANCH\", \"no_expiry\": true}}" \
    -p "$PROFILE" -o json | jq -r '"created: "+.name+" | state: "+(.status.current_state // "PENDING")'

  step "2. Wait for the branch + its auto endpoint to be READY/ACTIVE"
  for _ in $(seq 1 30); do
    ep=$(databricks postgres list-endpoints "$P/branches/$DEV_BRANCH" -p "$PROFILE" -o json 2>/dev/null \
         | jq -r '.[0].status.current_state // "PENDING"')
    echo "  endpoint: $ep"
    [ "$ep" = "ACTIVE" ] || [ "$ep" = "IDLE" ] && break
    sleep 5
  done

  step "3. Connect to the dev endpoint with psql (OAuth token as password)"
  local ep_name host tok user
  ep_name="$P/branches/$DEV_BRANCH/endpoints/primary"
  host=$(databricks postgres list-endpoints "$P/branches/$DEV_BRANCH" -p "$PROFILE" -o json | jq -r '.[0].status.hosts.host')
  tok=$(databricks postgres generate-database-credential "$ep_name" -p "$PROFILE" -o json | jq -r .token)
  user=$(databricks current-user me -p "$PROFILE" -o json | jq -r .userName)
  echo "  host=$host  user=$user"
  # NOTE: the token is passed via PGPASSWORD (env), never printed on screen.
  #       On stage, drop the -c flags to get an interactive psql prompt and type
  #       \dn, \dt claims.*, \df claims.rpc_*, SELECT ... yourself.
  PGPASSWORD="$tok" "$PSQL_BIN" \
    "host=$host port=5432 dbname=$DB user=$user sslmode=require" -X \
    -c "\dn" \
    -c "\dt claims.*" \
    -c "\df claims.rpc_*" \
    -c "SELECT count(*) AS agent_memory_rows FROM claims.agent_memory;"
  echo "  ^ schema, tables, RPCs and seeded rows were cloned — no DDL was run on dev."

  step "4. Prove isolation: write on dev, production is untouched"
  PGPASSWORD="$tok" "$PSQL_BIN" "host=$host port=5432 dbname=$DB user=$user sslmode=require" -X -q \
    -c "SELECT claims.rpc_write_memory('CLM-10001','databricks.claim_triage.v1','dev.experiment','{\"branch\":\"dev\"}','r','t');" >/dev/null
  echo "  wrote dev.experiment on dev"
  local phost ptok
  phost=$(databricks postgres list-endpoints "$P/branches/$SRC_BRANCH" -p "$PROFILE" -o json | jq -r '.[0].status.hosts.host')
  ptok=$(databricks postgres generate-database-credential "$P/branches/$SRC_BRANCH/endpoints/primary" -p "$PROFILE" -o json | jq -r .token)
  PGPASSWORD="$ptok" "$PSQL_BIN" "host=$phost port=5432 dbname=$DB user=$user sslmode=require" -X -t -A \
    -c "SELECT 'production dev.experiment rows: '||count(*) FROM claims.agent_memory WHERE memory_key='dev.experiment';"
  echo "  ^ 0 on production = the branch is isolated."

  if [ "$keep" = "keep" ]; then
    step "5. (kept) dev branch left in place — delete later with:"
    echo "  databricks postgres delete-branch $P/branches/$DEV_BRANCH -p $PROFILE"
  else
    step "5. Drop the branch (storage reclaimed; compute was scale-to-zero)"
    databricks postgres delete-branch "$P/branches/$DEV_BRANCH" -p "$PROFILE" && echo "  deleted $DEV_BRANCH"
  fi
}

part_b() {
  cat <<EOF

== Part B — branch-per-PR via GitHub Actions (automated) ==

A git push that opens/updates a PR triggers .github/workflows/lakebase-pr-branch.yml,
which (on the self-hosted runner in the allowed network) creates a Lakebase branch
'ci-pr-<n>' off production, verifies the RPC boundary on the clone, then deletes it
when the PR closes. Verified live on PR #4: create -> verify -> teardown.

Drive it live:

  1. Make any change on a new git branch and open a PR:
       git checkout -b demo/branch-per-pr
       echo "# trigger" >> README.md && git commit -am "demo: branch per PR" && git push -u origin demo/branch-per-pr
       gh pr create --fill

  2. Watch CI create the Lakebase branch (≈1-2 min):
       gh run watch \$(gh run list --branch demo/branch-per-pr --limit 1 --json databaseId --jq '.[0].databaseId')
       databricks postgres list-branches $P -p $PROFILE -o json | jq -r '.[].name'   # ci-pr-<n> appears

  3. Close the PR -> teardown deletes the branch:
       gh pr close <n>
       databricks postgres list-branches $P -p $PROFILE -o json | jq -r '.[].name'   # back to production

Prerequisites (already configured for $REPO):
  * repo secrets: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET
    (a service principal with CAN_MANAGE on the Lakebase project)
  * repo variable CI_RUNNER = <self-hosted-runner-label> (workspace IP ACLs block
    GitHub-hosted runners, so the live jobs run on a self-hosted runner in-network)
  * the self-hosted runner is online

Under the hood each step is: scripts/ci_lakebase_branch.py {create,test,destroy}.
EOF
}

case "${1:-a}" in
  a)      part_a no ;;
  a-keep) part_a keep ;;
  b)      part_b ;;
  *) echo "usage: $0 [a|a-keep|b]"; exit 2 ;;
esac
