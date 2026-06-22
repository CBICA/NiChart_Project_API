# Cloud-local testing

Run the NiChart API server on your local machine in `cloud` mode — with real
Cognito auth, real Lambda invocations, and real Batch/CloudWatch interactions —
without needing the full ECS deployment or an FSx mount.

---

## How it differs from a real cloud deployment

| Aspect | Cloud (ECS) | Cloud-local |
|---|---|---|
| JWT auth | Cognito (enforced) | Cognito (enforced — same pool) |
| Job submission | Lambda → Batch | Lambda → Batch (same Lambda) |
| Data root | `/fsx/fsx` inside container | Any host directory, mounted at `/fsx/fsx` inside container |
| Data durability | FSx for Lustre (S3-mirrored) | S3 directly — API server syncs before/after each step |
| Credentials | ECS task role (auto-refresh) | Assumed via `sts:AssumeRole` (expires after max session duration) |

**What you can test end-to-end:** auth, project/file management, Lambda submission,
full pipeline execution (Batch containers pull inputs from S3 and push outputs back —
the API server handles the sync automatically).

**What differs from production:** credentials are temporary STS tokens and expire;
there is no FSx layer so very large concurrent workloads won't benefit from FSx's
caching — for testing this doesn't matter.

---

## How S3 sync works

The Lambda already injects `aws s3 sync` at both ends of every Batch job command:

```
aws s3 sync s3://{bucket}/fsx/{user_id} /fsx/fsx/{user_id}  &&  <tool>  &&
aws s3 sync /fsx/fsx/{user_id} s3://{bucket}/fsx/{user_id}
```

In production, FSx transparently bridges this S3 bucket and the container's
filesystem. In cloud-local mode, the API server replaces FSx:

1. **Before submitting each step**: the API server uploads the project directory
   to `s3://{bucket}/fsx/{user_id}/{project}/` so the Batch job finds its inputs.
2. **After each step completes** (success or failure): the API server downloads
   from S3 so pipeline outputs appear locally and can be served via the files API.

This is all automatic when `NICHART_S3_DATA_BUCKET` is set (it is, in the
compose overlay). No manual `fsx-sync.sh` calls needed.

---

## One-time setup

### 1. Choose a data directory

Pick any directory on your machine. The overlay mounts it at `/fsx/fsx` inside
the container — that path is what the Lambda validates. The host path is arbitrary.

```bash
export NICHART_HOST_DATA_PATH=~/nichart-data
mkdir -p "$NICHART_HOST_DATA_PATH"
```

Add this export to your shell profile so it survives new terminals, or set it in
`.env.cloud-local` (the overlay picks it up via `--env-file`).

### 2. Create a dedicated IAM user for local testing

This gives you stable, long-lived credentials that can assume the ECS task role.
Note: IAM user ARNs (`arn:aws:iam::ACCOUNT:user/name`) **are** valid trust policy
principals — unlike SSO/federated session ARNs, which are not.

In the AWS Console (logged in via your normal SSO):

**a. Create the user**
IAM → Users → Create user → name: `nichart-local-dev` → no console access → Create

**b. Create access keys**
Click the user → Security credentials → Create access key →
select "Application running outside AWS" → save the key ID and secret (shown once)

**c. Attach an inline policy allowing the user to assume the task role**
On the user → Add permissions → Create inline policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::<account-id>:role/<task-role-name>"
  }]
}
```

**d. Add the user to the task role's trust policy**
IAM → Roles → (the task role) → Trust relationships → Edit →
add this statement alongside the existing `ecs-tasks` entry:
```json
{
  "Effect": "Allow",
  "Principal": {
    "AWS": "arn:aws:iam::<account-id>:user/nichart-local-dev"
  },
  "Action": "sts:AssumeRole"
}
```

**e. Configure the AWS CLI with the user's keys**
```bash
aws configure --profile nichart-local-dev
# access key ID     → paste from step b
# secret access key → paste from step b
# region            → us-east-1
# output format     → json
```

Verify:
```bash
aws sts get-caller-identity --profile nichart-local-dev
```

Find the task role ARN (you'll need it for the daily workflow):
```bash
aws ecs describe-task-definition \
  --task-definition <your-task-definition-name> \
  --query 'taskDefinition.taskRoleArn' \
  --output text \
  --profile nichart-local-dev
```

**f. (Optional) Increase the role's maximum session duration**
By default STS tokens expire after 1 hour. For longer sessions (up to 12 hours):
IAM → Roles → (the task role) → Edit → Maximum session duration → 6 hours.
Then update `scripts/export-cloud-creds.sh` has `--duration-seconds 21600`.

---

## Daily workflow

### Step 1 — Export credentials

```bash
scripts/export-cloud-creds.sh \
  --role-arn arn:aws:iam::123456789012:role/nichart-api-task-role \
  --profile nichart-local-dev
```

This calls `sts:AssumeRole`, writes temporary credentials to `.env.cloud-local`
(git-ignored), and prints the expiration time. Re-run when they expire.

`jq` must be installed (`sudo apt install jq` / `brew install jq`).

### Step 2 — Start the server

```bash
docker compose -f docker-compose.yml -f docker-compose.cloud-local.yml \
  --env-file .env.cloud-local up
```

The overlay:
- Sets `NICHART_EXECUTION_MODE=cloud` and `NICHART_DATA_ROOT=/fsx/fsx`
- Mounts `$NICHART_HOST_DATA_PATH` at `/fsx/fsx` inside the container
- Sets `NICHART_S3_DATA_BUCKET=cbica-nichart-io` to enable automatic S3 sync
- Passes the temporary `AWS_*` credentials into the container

The server starts on `http://localhost:8000`.

### Step 3 — Get a Cognito token

Cloud mode enforces auth on all non-public routes.

```bash
# Prompts for password; prints the ID token
TOKEN=$(python scripts/get-token.py your@email.com)

# Verify it works
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/projects
```

Tokens expire after 1 hour. Re-run the script to refresh.

### Step 4 — Run a pipeline end-to-end

```bash
PROJECT="my-test-project"

# 1. Create the project
curl -s -X POST http://localhost:8000/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"$PROJECT\"}" | jq

# 2. Upload a T1 NIfTI (two-step: stage then commit)
STAGE=$(curl -s -X POST "http://localhost:8000/projects/$PROJECT/files/upload/nifti" \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@/path/to/sub001_T1.nii.gz")
echo "$STAGE" | jq

STAGING_ID=$(echo "$STAGE" | jq -r '.staging_id')
curl -s -X POST "http://localhost:8000/projects/$PROJECT/files/stage/$STAGING_ID/commit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mappings": [{"filename": "sub001_T1.nii.gz", "mrid": "sub001", "modality": "t1"}]}' | jq

# 3. Submit the pipeline
#    The API server automatically uploads data to S3 before Batch sees the job.
RUN_ID=$(curl -s -X POST "http://localhost:8000/projects/$PROJECT/jobs/pipelines" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pipeline_id": "run_dlmuse"}' | jq -r '.run_id')
echo "Run: $RUN_ID"

# 4. Poll status (Batch runs on the cloud side)
curl -s "http://localhost:8000/jobs/pipelines/$RUN_ID" \
  -H "Authorization: Bearer $TOKEN" | jq '{status, jobs_ahead, estimated_wait_seconds}'

# 5. Wait for completion, then inspect results
#    Results are downloaded from S3 automatically after the step finishes.
curl -s "http://localhost:8000/projects/$PROJECT/results/run_dlmuse" \
  -H "Authorization: Bearer $TOKEN" | jq

# 6. Check logs if anything went wrong
curl -s "http://localhost:8000/jobs/pipelines/$RUN_ID/logs" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.logs'
```

No manual S3 sync steps needed — the API server handles it automatically.

---

## Manual S3 inspection

If you want to inspect or pre-populate data in S3 directly, `scripts/fsx-sync.sh`
is still available:

```bash
# Upload to S3 manually (e.g. to pre-seed data for a Batch-only test)
scripts/fsx-sync.sh up --user "$SUB" --project "$PROJECT"

# Download from S3 manually (e.g. to retrieve results if the server was restarted)
scripts/fsx-sync.sh down --user "$SUB" --project "$PROJECT"
```

Find your Cognito `sub`:
```bash
python3 -c "
import sys, base64, json
t = sys.argv[1].split('.')[1]
t += '=' * (4 - len(t) % 4)
print(json.loads(base64.b64decode(t))['sub'])
" "$TOKEN"
```

---

## Files involved

| File | Purpose |
|---|---|
| `docker-compose.cloud-local.yml` | Compose overlay — mounts data dir at `/fsx/fsx`, sets cloud env vars, enables S3 sync |
| `.env.cloud-local` | Temporary AWS credentials (git-ignored, written by the export script) |
| `.env.cloud-local.example` | Template — copy to `.env.cloud-local` if populating manually |
| `scripts/export-cloud-creds.sh` | Assumes the task role and writes `.env.cloud-local` |
| `scripts/get-token.py` | Signs in with Cognito email/password and prints an ID token |
| `scripts/fsx-sync.sh` | Manual S3 sync helper (not required for normal workflow) |

---

## Refreshing expired credentials

```bash
scripts/export-cloud-creds.sh \
  --role-arn arn:aws:iam::123456789012:role/nichart-api-task-role \
  --profile nichart-local-dev
docker compose -f docker-compose.yml -f docker-compose.cloud-local.yml \
  --env-file .env.cloud-local restart api
```

---

## Troubleshooting

**`Auth failed: ...` from get-token.py**
Check that `ALLOW_USER_PASSWORD_AUTH` is enabled on the Cognito app client
(`1ugglpalgp9r2gvb24s2v7dunq`): Cognito → User Pools → App clients → Edit → Auth flows.

**`Invalid token: Invalid audience` (HTTP 401 from the API)**
The token's `aud` claim doesn't match the client ID the server expects. Make sure
you're using `scripts/get-token.py` (which targets the correct client), not a token
issued by the old ALB app client.

**`An error occurred (AccessDenied) when calling the AssumeRole operation`**
Your IAM identity isn't in the task role's trust policy. See the one-time setup step above.

**`Lambda returned 403 / path not within allowed prefix`**
`NICHART_DATA_ROOT` inside the container isn't `/fsx/fsx`. Make sure you're using
both compose files and the overlay is applied correctly.

**`S3 pre-sync failed: ...` in pipeline logs**
The task role is missing S3 permissions on `cbica-nichart-io`. The role should
already have these for FSx access — check the attached policies in IAM.

**`[No log stream yet — job ... status: RUNNABLE]`**
The Batch job is still queued. Poll status a few times; the log stream only appears
once the container starts.

**`[CloudWatch error: ... AccessDeniedException]`**
The task role is missing `logs:GetLogEvents` on `/aws/batch/job`. Verify the IAM policy.
