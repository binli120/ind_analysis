#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

poetry run pytest \
  --import-mode=importlib \
  -o python_files='*.test.py test_*.py' \
  -o testpaths='src tests'
