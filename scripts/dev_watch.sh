#!/usr/bin/env bash
# Auto-build, test, and run uvicorn on every code change.
set -euo pipefail

if ! command -v entr >/dev/null 2>&1; then
  echo "entr is required (brew install entr or apt-get install entr)" >&2
  exit 1
fi

APP=${APP:-pdf_analysis.api.server:app}
HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8000}
UVICORN_ARGS=${UVICORN_ARGS:-"--reload --reload-dir src --reload-dir tests"}

BUILD_CMD=${BUILD_CMD:-"poetry build -q"}
TEST_CMD=${TEST_CMD:-"poetry run pytest"}
SERVE_CMD=${SERVE_CMD:-"poetry run uvicorn ${APP} --host ${HOST} --port ${PORT} ${UVICORN_ARGS}"}

echo "Watching src/ and tests/ for changes..."
echo "Build: ${BUILD_CMD}"
echo "Test:  ${TEST_CMD}"
echo "Serve: ${SERVE_CMD}"

find src tests -type f \( -name '*.py' -o -name '*.toml' -o -name '*.yaml' -o -name '*.yml' \) \
  | entr -r sh -c "${BUILD_CMD} && ${TEST_CMD} && ${SERVE_CMD}"
