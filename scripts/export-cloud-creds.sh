#!/usr/bin/env bash
# Assume the NiChart ECS task role and write temporary credentials to
# .env.cloud-local for use with docker-compose.cloud-local.yml.
#
# Usage:
#   scripts/export-cloud-creds.sh --role-arn <arn> [--profile <aws-profile>]
#
#   --role-arn   ARN of the ECS task role to assume (required)
#   --profile    AWS CLI profile whose credentials are used to call sts:AssumeRole
#                (default: the AWS_PROFILE env var, or the default profile)
#
# Example (using the dedicated IAM user — see docs/cloud-local-testing.md):
#   scripts/export-cloud-creds.sh \
#     --role-arn arn:aws:iam::123456789012:role/nichart-api-task-role \
#     --profile nichart-local-dev
#
# Requirements: AWS CLI, jq. Credentials expire after 1 hour; re-run to refresh.

set -euo pipefail

ROLE_ARN=""
PROFILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role-arn) ROLE_ARN="$2"; shift 2 ;;
    --profile)  PROFILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$ROLE_ARN" ]]; then
  echo "Usage: $0 --role-arn <arn> [--profile <aws-profile>]" >&2
  echo "" >&2
  echo "Find the task role ARN:" >&2
  echo "  aws ecs describe-task-definition --task-definition <task-def-name> \\" >&2
  echo "    --query 'taskDefinition.taskRoleArn' --output text [--profile <profile>]" >&2
  exit 1
fi

PROFILE_ARG=""
if [[ -n "$PROFILE" ]]; then
  PROFILE_ARG="--profile $PROFILE"
fi

echo "Assuming role: $ROLE_ARN"
[[ -n "$PROFILE" ]] && echo "Using profile: $PROFILE"

# shellcheck disable=SC2086
CREDS=$(aws sts assume-role \
  --role-arn "$ROLE_ARN" \
  --role-session-name nichart-local-cloud-test \
  --duration-seconds 21600 \
  --output json \
  $PROFILE_ARG)

ACCESS_KEY=$(echo "$CREDS"   | jq -r '.Credentials.AccessKeyId')
SECRET_KEY=$(echo "$CREDS"   | jq -r '.Credentials.SecretAccessKey')
SESSION_TOKEN=$(echo "$CREDS" | jq -r '.Credentials.SessionToken')
EXPIRATION=$(echo "$CREDS"   | jq -r '.Credentials.Expiration')

cat > .env.cloud-local <<EOF
AWS_ACCESS_KEY_ID=${ACCESS_KEY}
AWS_SECRET_ACCESS_KEY=${SECRET_KEY}
AWS_SESSION_TOKEN=${SESSION_TOKEN}
AWS_DEFAULT_REGION=us-east-1
EOF

echo "Credentials written to .env.cloud-local (expire: ${EXPIRATION})"
echo ""
echo "Start the cloud-local stack:"
echo "  sudo mkdir -p /fsx/fsx && sudo chown \$USER /fsx/fsx  # one-time"
echo "  docker compose -f docker-compose.yml -f docker-compose.cloud-local.yml \\"
echo "    --env-file .env.cloud-local up"
