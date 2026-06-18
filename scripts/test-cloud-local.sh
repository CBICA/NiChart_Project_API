#!/usr/bin/env bash
# End-to-end cloud-local test:
#   upload a T1 NIfTI → sync to S3 → submit DLMUSE pipeline → poll → logs → sync results
#
# Prerequisites (one-time setup — see docs/cloud-local-testing.md):
#   1. /fsx/fsx exists:  sudo mkdir -p /fsx/fsx && sudo chown $USER:$USER /fsx/fsx
#   2. IAM creds exported: scripts/export-cloud-creds.sh --role-arn ... --profile nichart-local-dev
#      → writes .env.cloud-local
#   3. API server running in a separate terminal:
#      docker compose -f docker-compose.yml -f docker-compose.cloud-local.yml \
#        --env-file .env.cloud-local up
#
# Usage:
#   scripts/test-cloud-local.sh [--email EMAIL] [--t1 FILE] [--project NAME]
#                               [--pipeline ID] [--api URL] [--poll N]

set -euo pipefail

API="${NICHART_API_URL:-http://localhost:8000}"
PIPELINE="run_dlmuse"
POLL_INTERVAL=30
EMAIL=""
T1_FILE=""
PROJECT=""

# ── Parse flags ───────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email)    EMAIL="$2";    shift 2 ;;
    --t1)       T1_FILE="$2";  shift 2 ;;
    --project)  PROJECT="$2";  shift 2 ;;
    --pipeline) PIPELINE="$2"; shift 2 ;;
    --api)      API="$2";      shift 2 ;;
    --poll)     POLL_INTERVAL="$2"; shift 2 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

die()  { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "──────────────────────────────────────────"; echo "  $*"; echo "──────────────────────────────────────────"; }
ok()   { echo "  ✔ $*"; }

json_field() {
  # json_field <json_string> <field_name>
  python3 -c "import sys,json; print(json.loads(sys.argv[1]).get('$2',''))" "$1"
}

# ── Preflight ─────────────────────────────────────────────────────────────────

step "Preflight checks"

[[ -f .env.cloud-local ]] || die ".env.cloud-local not found. Run scripts/export-cloud-creds.sh first."
ok ".env.cloud-local present"

[[ -d /fsx/fsx ]] || die "/fsx/fsx does not exist. Run: sudo mkdir -p /fsx/fsx && sudo chown \$USER:\$USER /fsx/fsx"
ok "/fsx/fsx exists"

curl -sf "$API/health" > /dev/null \
  || die "API server not reachable at $API. Start it with:
    docker compose -f docker-compose.yml -f docker-compose.cloud-local.yml --env-file .env.cloud-local up"
ok "API server reachable at $API"

# ── Collect inputs ────────────────────────────────────────────────────────────

step "Inputs"

if [[ -z "$EMAIL" ]]; then
  read -r -p "  Cognito email: " EMAIL
fi
echo "  Email   : $EMAIL"

if [[ -z "$T1_FILE" ]]; then
  read -r -p "  T1 NIfTI file path (.nii or .nii.gz): " T1_FILE
fi
[[ -f "$T1_FILE" ]] || die "File not found: $T1_FILE"
echo "  T1 file : $T1_FILE"

if [[ -z "$PROJECT" ]]; then
  PROJECT="dlmuse-test-$(date +%Y%m%d-%H%M%S)"
fi
echo "  Project : $PROJECT"
echo "  Pipeline: $PIPELINE"

# ── Authentication ────────────────────────────────────────────────────────────

step "Step 1/7  —  Cognito authentication"
echo "  (You will be prompted for your Cognito password)"
echo

TOKEN=$(python3 scripts/get-token.py "$EMAIL")
ok "ID token obtained"

# Decode the Cognito sub from the ID token
SUB=$(python3 -c "
import sys, base64, json
t = sys.argv[1].split('.')[1]
t += '=' * (4 - len(t) % 4)
print(json.loads(base64.b64decode(t))['sub'])
" "$TOKEN")
ok "Cognito sub: $SUB"

AUTH_HEADER=(-H "Authorization: Bearer $TOKEN")

# ── Create project ────────────────────────────────────────────────────────────

step "Step 2/7  —  Create project '$PROJECT'"
RESP=$(curl -sf -X POST "$API/projects" \
  "${AUTH_HEADER[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"$PROJECT\"}")
ok "Created: $(json_field "$RESP" id)"

# ── Upload NIfTI ──────────────────────────────────────────────────────────────

step "Step 3/7  —  Upload T1 NIfTI"
FILENAME=$(basename "$T1_FILE")
STAGE_RESP=$(curl -sf -X POST "$API/projects/$PROJECT/files/upload/nifti" \
  "${AUTH_HEADER[@]}" \
  -F "files=@$T1_FILE")

STAGING_ID=$(json_field "$STAGE_RESP" staging_id)
# Extract proposal fields via python (nested JSON)
read -r INFERRED_MRID INFERRED_MOD <<< "$(python3 -c "
import sys, json
d = json.loads(sys.argv[1])
p = d['proposals'][0]
print(p.get('inferred_mrid') or '', p.get('inferred_modality') or '')
" "$STAGE_RESP")"

echo "  Staging ID : $STAGING_ID"
echo "  Filename   : $FILENAME"
echo "  Inferred MRID    : ${INFERRED_MRID:-<not detected>}"
echo "  Inferred modality: ${INFERRED_MOD:-<not detected>}"

MRID="${INFERRED_MRID:-sub001}"
MODALITY="${INFERRED_MOD:-t1}"

if [[ -z "$INFERRED_MRID" || -z "$INFERRED_MOD" ]]; then
  echo
  echo "  Could not fully infer metadata from filename '$FILENAME'."
  [[ -z "$MRID"     ]] && read -r -p "  Enter MRID (e.g. sub001): " MRID
  [[ "$MODALITY" == "t1" ]] || read -r -p "  Enter modality (t1/fl/t2/t1ce/adc) [t1]: " MOD && MODALITY="${MOD:-t1}"
fi
ok "Will commit as MRID='$MRID', modality='$MODALITY'"

# ── Commit staging ────────────────────────────────────────────────────────────

step "Step 4/7  —  Commit staging"
COMMIT_PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({'mappings': [{'filename': sys.argv[1], 'mrid': sys.argv[2], 'modality': sys.argv[3]}]}))
" "$FILENAME" "$MRID" "$MODALITY")

COMMIT_RESP=$(curl -sf -X POST "$API/projects/$PROJECT/files/stage/$STAGING_ID/commit" \
  "${AUTH_HEADER[@]}" \
  -H "Content-Type: application/json" \
  -d "$COMMIT_PAYLOAD")

COMMITTED_PATH=$(python3 -c "
import sys, json
d = json.loads(sys.argv[1])
print(d['committed'][0]['path'])
" "$COMMIT_RESP")
ok "Committed to: $COMMITTED_PATH"

# ── Sync up to S3 ─────────────────────────────────────────────────────────────

step "Step 5/7  —  Sync local data → S3 (so Batch can reach it)"
echo "  Running: scripts/fsx-sync.sh up --user '$SUB' --project '$PROJECT'"
scripts/fsx-sync.sh up --user "$SUB" --project "$PROJECT"
ok "Sync complete"

# ── Submit pipeline ───────────────────────────────────────────────────────────

step "Step 6/7  —  Submit pipeline '$PIPELINE'"
SUBMIT_RESP=$(curl -sf -X POST "$API/projects/$PROJECT/jobs/pipelines" \
  "${AUTH_HEADER[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"pipeline_id\": \"$PIPELINE\", \"reuse_cached_steps\": false}")

RUN_ID=$(json_field "$SUBMIT_RESP" run_id)
[[ -n "$RUN_ID" ]] || die "Submission failed: $SUBMIT_RESP"
ok "Run ID: $RUN_ID"

# ── Poll for completion ───────────────────────────────────────────────────────

step "Step 7/7  —  Polling status (every ${POLL_INTERVAL}s — Ctrl-C to stop)"
echo "  Batch jobs go: RUNNABLE → STARTING → RUNNING → SUCCEEDED/FAILED"
echo "  Log stream appears once the container starts."
echo

PREV_STATUS=""
while true; do
  STATUS_RESP=$(curl -sf "$API/jobs/pipelines/$RUN_ID" "${AUTH_HEADER[@]}")
  STATUS=$(json_field "$STATUS_RESP" status)
  STEP_INFO=$(python3 -c "
import sys, json
d = json.loads(sys.argv[1])
cur = d.get('current_step', 0)
tot = d.get('total_steps', 0)
err = d.get('error') or ''
if err:
    print(f'step {cur}/{tot}  error: {err}')
else:
    print(f'step {cur}/{tot}')
" "$STATUS_RESP")

  if [[ "$STATUS" != "$PREV_STATUS" ]]; then
    echo "  [$(date '+%H:%M:%S')]  $STATUS  ($STEP_INFO)"
    PREV_STATUS="$STATUS"
  else
    printf "  [$(date '+%H:%M:%S')]  $STATUS  ($STEP_INFO)\r"
  fi

  if [[ "$STATUS" == "succeeded" || "$STATUS" == "failed" ]]; then
    echo
    break
  fi
  sleep "$POLL_INTERVAL"
done

# ── Logs ──────────────────────────────────────────────────────────────────────

echo
echo "── CloudWatch logs ──────────────────────────────────────────────────────"
curl -sf "$API/jobs/pipelines/$RUN_ID/logs" "${AUTH_HEADER[@]}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('logs','(no logs)'))"

# ── Sync results back down ────────────────────────────────────────────────────

if [[ "$STATUS" == "succeeded" ]]; then
  echo
  echo "── Syncing results S3 → local ───────────────────────────────────────────"
  scripts/fsx-sync.sh down --user "$SUB" --project "$PROJECT"
  echo
  ok "Results synced. Browse with:"
  echo "    curl -s '$API/projects/$PROJECT/files' -H 'Authorization: Bearer \$TOKEN'"
else
  echo
  echo "  Pipeline did not succeed (status=$STATUS). Results not synced."
  echo "  Check the logs above and re-run after fixing the issue."
fi

echo
echo "Run ID (save this): $RUN_ID"
