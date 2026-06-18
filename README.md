# NiChart Project API

FastAPI backend for the NiChart medical-imaging pipeline platform. It replaces the
Streamlit application with a clean REST API consumed by the React UI (`../NiChart_UI`).

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) ≥ 24
- [Docker Compose](https://docs.docker.com/compose/) v2 (included with Docker Desktop)

No host Python installation is required. All development and testing runs inside Docker.

---

## Quick start — local mode

Local mode runs pipeline jobs via the Docker daemon on your machine and stores all data
on the local filesystem. No AWS credentials are needed.

```bash
# 1. Copy the example environment file and adjust if needed
cp .env.example .env

# 2. Start the development server (hot-reload enabled)
docker compose up
```

The API is now available at <http://localhost:8000>.
Interactive docs: <http://localhost:8000/docs>

To stop the server press `Ctrl-C`, then:

```bash
docker compose down
```

### Data directory

By default data is stored in a Docker named volume (`nichart-data`) mounted at `/data`
inside the container. To use a host directory instead, set `NICHART_DATA_ROOT` to an
absolute path in your `.env` file and update the `docker-compose.yml` volume mount
accordingly.

Sample data and any local data directories (`data/`, `sample_data/`) are excluded from
git and from Docker image builds via `.gitignore` and `.dockerignore`.

---

## Running the tests

```bash
# Build the dev image (cached after first run)
docker build --target dev -t nichart-api-dev .

# Run the full test suite
docker run --rm -e NICHART_EXECUTION_MODE=local nichart-api-dev pytest -v
```

Or use the helper script:

```bash
bash scripts/test.sh
```

---

## Helper scripts

| Script | What it does |
|--------|-------------|
| `scripts/dev.sh` | `docker compose up` — starts the dev server |
| `scripts/test.sh` | Builds the dev image and runs `pytest -v` |

---

## Dummy / synthetic job

`resources/tools/dummy_sleep.yaml` defines a no-op tool that runs `sleep N` inside an
Alpine container. Use it to exercise the full pipeline API (submit → poll → complete)
without needing any imaging data.

`resources/pipelines/dummy_pipeline.yaml` wraps it in a single-step pipeline named
`dummy_pipeline` with a default duration of 10 seconds.

Once the job backends are implemented you can submit it via:

```http
POST /projects/{project_id}/jobs/pipelines
{"pipeline_id": "dummy_pipeline"}
```

---

## Cloud mode

Cloud mode routes pipeline jobs to AWS Batch via the Lambda function
`cbica-nichart-submitjob` and expects an AWS Cognito ID token on every request
(except public endpoints).

### Required environment variables

| Variable | Description |
|----------|-------------|
| `NICHART_EXECUTION_MODE` | Set to `cloud` |
| `NICHART_BATCH_QUEUE_NAME` | AWS Batch job queue name (default: `nichart-jobs`) |
| `NICHART_LAMBDA_FUNCTION_NAME` | Lambda function name (default: `cbica-nichart-submitjob`) |
| `NICHART_COGNITO_REGION` | AWS region (default: `us-east-1`) |
| `NICHART_COGNITO_USER_POOL_ID` | Cognito User Pool ID |

### AWS credentials

The server needs an IAM role or credentials with:
- `lambda:InvokeFunction` on `cbica-nichart-submitjob`
- `batch:ListJobs` and `batch:DescribeJobs` on the job queue (for `GET /cloud/status`)

In ECS / EC2 these are typically provided via an instance/task role. Locally, set
`AWS_PROFILE` or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in your `.env`.

### Lambda interface — `cbica-nichart-submitjob`

The batch backend invokes the Lambda with:

```json
{
  "id_token": "<Cognito ID token>",
  "tool_name": "nichart_dlmuse",
  "user_mounts": {
    "input":  "/fsx/fsx/{user_sub}/{project}/t1",
    "output": "/fsx/fsx/{user_sub}/{project}/dlmuse_vol"
  },
  "user_params": { "param_name": "value" },
  "num_subjects": 42
}
```

**The Lambda is the security boundary for command construction.** It loads the tool spec
from S3 (`cbica-nichart-staticfiles/tools/{tool_name}.yaml`), validates that all mount
paths are within the caller's FSx directory (`/fsx/fsx/{user_sub}/`), and generates the
Docker command itself. The caller never specifies an image or command — doing so would
let users run arbitrary workloads on cloud infrastructure.

The resulting Batch job stores these parameters (readable via `describe_jobs`):

| Batch parameter | Value |
|-----------------|-------|
| `tool_id` | `tool_name` from the request |
| `num_subjects` | `num_subjects` from the request |
| `FullCommand` | Generated Docker command (Lambda-internal) |
| `ContainerImage` | Container image from the tool YAML (Lambda-internal) |

`tool_id` and `num_subjects` are used by `GET /cloud/status` to compute the queue-drain
estimate.

### Cloud queue status

`GET /cloud/status` is a public endpoint that returns the current Batch queue depth and
a rough estimate of time until the queue drains:

```http
GET /cloud/status

{
  "mode": "cloud",
  "queue_name": "nichart-jobs",
  "running_job_count": 3,
  "pending_job_count": 12,
  "estimated_queue_drain_seconds": 7200.0
}
```

The estimate is: `Σ (time_per_subject_seconds × num_subjects)` over all running and
pending jobs. `time_per_subject_seconds` comes from the `time_per_subject_seconds` field
in each tool's YAML definition; jobs for tools without this field are excluded from the
estimate.

---

## Configuration reference

See [`.env.example`](.env.example) for the full list of environment variables with
descriptions and defaults.

---

## API reference

Interactive Swagger UI: <http://localhost:8000/docs>  
ReDoc: <http://localhost:8000/redoc>  
OpenAPI JSON: <http://localhost:8000/openapi.json>
