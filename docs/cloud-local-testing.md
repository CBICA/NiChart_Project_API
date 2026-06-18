# Cloud-local testing

Run the NiChart API server on your local machine in `cloud` mode — with real
Cognito auth, real Lambda invocations, and real Batch/CloudWatch interactions —
without needing the full ECS deployment.

---

## How it differs from a real cloud deployment

| Aspect | Cloud (ECS) | Cloud-local |
|---|---|---|
| JWT auth | Cognito (enforced) | Cognito (enforced — same pool) |
| Job submission | Lambda → Batch | Lambda → Batch (same Lambda) |
| Data root | `/fsx/fsx` on FSx | `/fsx/fsx` on your local disk |
| Batch job data | Available on FSx | **Not available** — Batch jobs will start but fail because input data doesn't exist on FSx |
| Credentials | ECS task role (auto) | Assumed via `sts:AssumeRole` |

**What you can test:** auth enforcement, project/file management endpoints,
Lambda submission, Batch job queuing, Batch status polling, CloudWatch log
retrieval.

**What you cannot test end-to-end:** pipeline execution (Batch containers won't
find your locally-created data on FSx unless you separately upload it there).

---

## One-time setup

### 1. Create `/fsx/fsx` locally

The API sends data paths under `/fsx/fsx/{user_sub}/...` to the Lambda.
The Lambda validates they fall within that prefix, so the path must match
exactly. Create the directory on your machine:

```bash
sudo mkdir -p /fsx/fsx
sudo chown $USER:$USER /fsx/fsx
```

The compose overlay mounts this directory into the container at the same path.

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

---

## Daily workflow

### Step 1 — Export credentials (refresh hourly)

```bash
scripts/export-cloud-creds.sh \
  --role-arn arn:aws:iam::123456789012:role/nichart-api-task-role \
  --profile nichart-local-dev
```

This calls `sts:AssumeRole`, writes the temporary credentials to `.env.cloud-local`
(git-ignored), and prints the expiration time. Re-run when they expire.

`jq` must be installed (`sudo apt install jq` / `brew install jq`).

### Step 2 — Start the server

```bash
docker compose -f docker-compose.yml -f docker-compose.cloud-local.yml \
  --env-file .env.cloud-local up
```

The overlay:
- Overrides `NICHART_EXECUTION_MODE=cloud` and `NICHART_DATA_ROOT=/fsx/fsx`
- Passes the `AWS_*` credentials from `.env.cloud-local` into the container
- Mounts `/fsx/fsx` at the same path inside the container

The server starts on `http://localhost:8000`.

### Step 3 — Get Cognito tokens

Cloud mode enforces auth on all non-public routes. Use `scripts/get-token.py`
to sign in with your Cognito email and password.

The API server validates the **ID token** (`Authorization: Bearer`), but the
Lambda verifies the caller via Cognito's `GetUser` API which requires an
**access token**. Fetch both in one call:

```bash
# Prompts for password; sets TOKEN (ID token) and ACCESS_TOKEN in the shell
eval $(python scripts/get-token.py your@email.com --env)

# Regular API requests only need the ID token
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/projects

# Pipeline submission requires both — the access token is forwarded to Lambda
curl -H "Authorization: Bearer $TOKEN" \
     -H "X-Access-Token: $ACCESS_TOKEN" \
     -X POST http://localhost:8000/projects/myproject/jobs/pipelines ...
```

Tokens are valid for 1 hour by default. Re-run and re-`eval` to refresh.
No AWS credentials or browser flow required — the script calls Cognito's
HTTPS API directly.

### Step 4 — Test pipeline submission

FSx for Lustre mirrors `s3://cbica-nichart-io/fsx/`. Batch jobs read input
from and write output to FSx, so local data must be uploaded to S3 first, and
results must be downloaded afterwards. `scripts/fsx-sync.sh` handles both
directions using the task role credentials in `.env.cloud-local`.

**Find your Cognito sub** (the directory name under `/fsx/fsx/`):
```bash
# After creating at least one project through the API, your sub dir appears:
ls /fsx/fsx/

# Or decode it directly from the token:
python3 -c "
import sys, base64, json
t = sys.argv[1].split('.')[1]
t += '=' * (4 - len(t) % 4)
print(json.loads(base64.b64decode(t))['sub'])
" "$TOKEN"
```

**Full test workflow:**
```bash
SUB="<your-cognito-sub>"
PROJECT="my-test-project"

# 1. Create the project
curl -s -X POST http://localhost:8000/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"$PROJECT\"}" | jq

# 2. Upload imaging data locally (e.g. a T1 NIfTI)
curl -s -X POST "http://localhost:8000/projects/$PROJECT/files/upload/nifti" \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@/path/to/sub001_T1.nii.gz" | jq
# ... then commit the staging proposal via the commit endpoint

# 3. Sync local data up to S3 so Batch can see it
scripts/fsx-sync.sh up --user "$SUB" --project "$PROJECT"

# 4. Submit the pipeline (Lambda invoked → Batch job queued)
#    X-Access-Token is required so the Lambda can verify you via Cognito GetUser
RUN_ID=$(curl -s -X POST "http://localhost:8000/projects/$PROJECT/jobs/pipelines" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Access-Token: $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pipeline_id": "run_dlmuse"}' | jq -r '.run_id')

# 5. Poll until the job finishes (Batch runs on the cloud side)
curl -s "http://localhost:8000/jobs/pipelines/$RUN_ID" \
  -H "Authorization: Bearer $TOKEN" | jq '.status'

# 6. Check CloudWatch logs
curl -s "http://localhost:8000/jobs/pipelines/$RUN_ID/logs" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.logs'

# 7. Sync results back down from S3
scripts/fsx-sync.sh down --user "$SUB" --project "$PROJECT"

# 8. Results are now visible via the files API
curl -s "http://localhost:8000/projects/$PROJECT/files" \
  -H "Authorization: Bearer $TOKEN" | jq
```

---

## Files involved

| File | Purpose |
|---|---|
| `docker-compose.cloud-local.yml` | Compose overlay — mounts `/fsx/fsx`, sets cloud env vars, passes credentials |
| `.env.cloud-local` | Temporary AWS credentials (git-ignored, written by the script) |
| `.env.cloud-local.example` | Template — copy to `.env.cloud-local` if populating manually |
| `scripts/export-cloud-creds.sh` | Assumes the task role and writes `.env.cloud-local` |
| `scripts/get-token.py` | Signs in with Cognito email/password and prints an ID token |
| `scripts/fsx-sync.sh` | Syncs `/fsx/fsx/` ↔ `s3://cbica-nichart-io/fsx/` (up before submit, down after) |

---

## Refreshing expired credentials

Credentials are valid for 1 hour. When they expire:

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
`NICHART_DATA_ROOT` isn't `/fsx/fsx`. The overlay sets this automatically; make sure you're
using both compose files.

**`[No log stream yet — job ... status: RUNNABLE]`**
The Batch job is still queued. Poll status a few times; the log stream only appears
once the container starts.

**`[CloudWatch error: ... AccessDeniedException]`**
The task role is missing `logs:GetLogEvents` on `/aws/batch/job`. Verify the IAM policy.

**`An error occurred (AccessDenied) when calling the ListObjectsV2 operation` (fsx-sync.sh)**
The task role is missing S3 permissions on `cbica-nichart-io`. The role should already
have these for FSx access — check the attached policies in IAM.
