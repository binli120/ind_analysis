#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POETRY_BIN="${POETRY:-poetry}"

usage() {
  cat <<USAGE
Usage: $(basename "$0") <command> [args...]

Commands:
  build           Build the package via poetry
  run             Run the FastAPI server (forwards extra args to uvicorn)
  lint            Run static checks (mypy)
  test            Execute the unittest suite
  help            Show this message

Environment variables:
  POETRY          Override the poetry executable (default: poetry)
USAGE
}

require_poetry() {
  if ! command -v "$POETRY_BIN" >/dev/null 2>&1; then
    echo "[ERROR] poetry is required but was not found in PATH. Set POETRY env var if installed elsewhere." >&2
    exit 1
  fi
}

command_build() {
  require_poetry
  (cd "$PROJECT_ROOT" && "$POETRY_BIN" build)
}

command_run() {
  require_poetry
  shift || true
  exec "$POETRY_BIN" run uvicorn pdf_analysis.api.server:app --host 0.0.0.0 --port "${PORT:-8000}" "$@"
}

command_lint() {
  require_poetry
  (cd "$PROJECT_ROOT" && "$POETRY_BIN" run mypy src tests)
}

command_test() {
  require_poetry
  shift || true
  (cd "$PROJECT_ROOT" && "$POETRY_BIN" run python -m unittest discover -s tests -p "test_*.py")
}

main() {
  if [[ $# -lt 1 ]]; then
    usage
    exit 1
  fi

  case "$1" in
    build)
      command_build "$@"
      ;;
    run)
      command_run "$@"
      ;;
    lint)
      command_lint "$@"
      ;;
    test)
      command_test "$@"
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      echo "[ERROR] Unknown command: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
