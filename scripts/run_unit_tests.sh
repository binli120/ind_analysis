#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

TEST_DIRS=()
while IFS= read -r dir; do
  TEST_DIRS+=("$dir")
done < <(find "$REPO_ROOT/src" -type d -name "__test__" | sort)

if [[ -d "$REPO_ROOT/tests" ]]; then
  TEST_DIRS+=("$REPO_ROOT/tests")
fi

if [[ ${#TEST_DIRS[@]} -eq 0 ]]; then
  TEST_DIRS=("$REPO_ROOT/src" "$REPO_ROOT/tests")
fi

poetry run pytest \
  --import-mode=importlib \
  -o python_files='*.test.py test_*.py' \
  "${TEST_DIRS[@]}"
