#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/tmp}"
SECTION_OUTPUT_DIR="${SECTION_OUTPUT_DIR:-$OUTPUT_DIR/ctd-2.6-templates}"
BUCKET="${BUCKET:-indtemplates}"
SECTIONS="${SECTIONS:-2.6.1,2.6.2,2.6.3,2.6.4,2.6.5,2.6.6,2.6.7}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Generate CTD DOCX templates and upload them to S3.

Environment variables:
  BUCKET=indtemplates
  OUTPUT_DIR=./tmp
  SECTION_OUTPUT_DIR=./tmp/ctd-2.6-templates
  SECTIONS=2.6.1,2.6.2,2.6.3,2.6.4,2.6.5,2.6.6,2.6.7
  AWS_PROFILE=profile-name (optional)
EOF
  exit 0
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI not found. Install it and retry." >&2
  exit 1
fi

if ! command -v python >/dev/null 2>&1; then
  echo "python not found. Install it and retry." >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR" "$SECTION_OUTPUT_DIR"

python "$ROOT_DIR/scripts/generate_ctd_template_docx.py" \
  --module 2.4 \
  --output "$OUTPUT_DIR/ctd-2.4-template.docx"

python "$ROOT_DIR/scripts/generate_ctd_template_docx.py" \
  --module 2.6-written \
  --output "$OUTPUT_DIR/ctd-2.6-written-template.docx"

python "$ROOT_DIR/scripts/generate_ctd_template_docx.py" \
  --module 2.6-tabulated \
  --output "$OUTPUT_DIR/ctd-2.6-tabulated-template.docx"

python "$ROOT_DIR/scripts/generate_ctd_template_docx.py" \
  --sections "$SECTIONS" \
  --output-dir "$SECTION_OUTPUT_DIR"

AWS_CMD=(aws)
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_CMD+=(--profile "$AWS_PROFILE")
fi

"${AWS_CMD[@]}" s3 cp "$OUTPUT_DIR/ctd-2.4-template.docx" "s3://$BUCKET/2.4/"
"${AWS_CMD[@]}" s3 cp "$OUTPUT_DIR/ctd-2.6-written-template.docx" "s3://$BUCKET/2.6/"
"${AWS_CMD[@]}" s3 cp "$OUTPUT_DIR/ctd-2.6-tabulated-template.docx" "s3://$BUCKET/2.6/"
"${AWS_CMD[@]}" s3 cp "$SECTION_OUTPUT_DIR/" "s3://$BUCKET/2.6/" \
  --recursive --exclude "*" --include "*.docx"
