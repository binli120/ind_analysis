#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENAPI_OUT="${OPENAPI_OUT:-$ROOT_DIR/dist/openapi.json}"
POSTMAN_OUT="${POSTMAN_OUT:-$ROOT_DIR/postman/postman_openapi_collection.json}"
POSTMAN_TOOL="${POSTMAN_TOOL:-openapi-to-postmanv2}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Generate OpenAPI JSON and a Postman collection from it.

Environment variables:
  OPENAPI_OUT=dist/openapi.json
  POSTMAN_OUT=postman/postman_openapi_collection.json
  POSTMAN_TOOL=openapi-to-postmanv2

If openapi-to-postmanv2 is not installed, the script will try to use:
  npx -y openapi-to-postmanv2
EOF
  exit 0
fi

python "$ROOT_DIR/scripts/generate_openapi.py" --output "$OPENAPI_OUT"

mkdir -p "$(dirname "$POSTMAN_OUT")"

if command -v "$POSTMAN_TOOL" >/dev/null 2>&1; then
  "$POSTMAN_TOOL" -s "$OPENAPI_OUT" -o "$POSTMAN_OUT"
elif command -v npx >/dev/null 2>&1; then
  npx -y "$POSTMAN_TOOL" -s "$OPENAPI_OUT" -o "$POSTMAN_OUT"
else
  echo "Missing $POSTMAN_TOOL and npx. Install openapi-to-postmanv2 or Node.js." >&2
  exit 1
fi

echo "OpenAPI written to $OPENAPI_OUT"
echo "Postman collection written to $POSTMAN_OUT"
