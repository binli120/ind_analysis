#!/usr/bin/env bash
  set -euo pipefail

  REPO="/Users/blee/dev/filynai/pdf-analysis"
  cd "$REPO"

  # Required environment (adjust to your setup)
  export AWS_REGION="us-east-1"
  export DEFAULT_TENANT_ID="c38daae8-07a8-4da4-9a68-9a9955b09f70"
  export DEFAULT_USER_ID="a5655137-b72b-4a24-a270-5cfe63cf5ea0"
  export DATABASE_URL="postgresql://postgres.zmakvebsjbwaitzxuogb:CXl5nkmY7vxp72gz@aws-0-us-east-2.pooler.supabase.com:5432/postgres"

  # Use full path to poetry if needed (run `which poetry`)
  /usr/local/bin/poetry run python scripts/ingest_module4_batch.py \
    --bucket doc-repository-dev \
    --prefix filynai.com/ \
    --all-modules
