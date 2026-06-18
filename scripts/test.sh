#!/usr/bin/env bash
# Build the dev image and run the full test suite.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build --target dev -t nichart-api-dev .
docker run --rm -e NICHART_EXECUTION_MODE=local nichart-api-dev pytest -v "$@"
