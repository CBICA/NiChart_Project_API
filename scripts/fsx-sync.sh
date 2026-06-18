#!/usr/bin/env bash
# Sync data between local /fsx/fsx and s3://cbica-nichart-io/fsx/.
#
# FSx for Lustre mirrors the S3 bucket, so syncing local ↔ S3 is equivalent
# to syncing local ↔ FSx (Batch jobs see the same data via FSx on the server).
#
# Usage:
#   scripts/fsx-sync.sh up   [--user <cognito-sub>] [--project <name>]
#   scripts/fsx-sync.sh down [--user <cognito-sub>] [--project <name>]
#
#   up   — copy local /fsx/fsx/... → S3 (before submitting a pipeline job)
#   down — copy S3 → local /fsx/fsx/... (after a pipeline job completes)
#
# Without --user / --project the sync covers the full /fsx/fsx/ tree.
# Scope it down when possible to avoid touching other users' data.
#
# Credentials are read from .env.cloud-local (written by export-cloud-creds.sh).
# The task role already has the S3 permissions needed for FSx access.
#
# Finding your Cognito sub (user directory name under /fsx/fsx/):
#   ls /fsx/fsx/          — shows dirs created after first login
#   python scripts/get-token.py you@email.com | python3 -c \
#     "import sys,base64,json; t=sys.stdin.read().strip().split('.')[1];
#      t+='='*(4-len(t)%4); print(json.loads(base64.b64decode(t))['sub'])"

set -euo pipefail

S3_BUCKET="cbica-nichart-io"
S3_PREFIX="fsx"
LOCAL_ROOT="/fsx/fsx"

DIRECTION=""
USER_SUB=""
PROJECT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    up|down)   DIRECTION="$1"; shift ;;
    --user)    USER_SUB="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$DIRECTION" ]]; then
  echo "Usage: $0 <up|down> [--user <sub>] [--project <name>]" >&2
  exit 1
fi

# Load task role credentials from .env.cloud-local
ENV_FILE="$(dirname "$0")/../.env.cloud-local"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env.cloud-local not found. Run scripts/export-cloud-creds.sh first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Build sync paths
LOCAL_PATH="$LOCAL_ROOT"
S3_PATH="s3://${S3_BUCKET}/${S3_PREFIX}"

[[ -n "$USER_SUB" ]]  && LOCAL_PATH="$LOCAL_PATH/$USER_SUB"  && S3_PATH="$S3_PATH/$USER_SUB"
[[ -n "$PROJECT" ]]   && LOCAL_PATH="$LOCAL_PATH/$PROJECT"   && S3_PATH="$S3_PATH/$PROJECT"

LOCAL_PATH="${LOCAL_PATH%/}/"
S3_PATH="${S3_PATH%/}/"

echo "Direction : $DIRECTION"
echo "Local     : $LOCAL_PATH"
echo "S3        : $S3_PATH"
echo ""

if [[ "$DIRECTION" == "up" ]]; then
  aws s3 sync "$LOCAL_PATH" "$S3_PATH"
else
  aws s3 sync "$S3_PATH" "$LOCAL_PATH"
fi

echo ""
echo "Sync complete."
