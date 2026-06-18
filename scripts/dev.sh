#!/usr/bin/env bash
# Start the development server with hot-reload.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up
