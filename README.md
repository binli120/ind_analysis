# Project Vision

This project provides an end‑to‑end workflow for turning complex PDF study reports into editable, analysable artefacts. The aim is to let subject-matter experts pull the raw content into collaborative tooling (markdown or rich-text editors), review and modify tables, perform lightweight analytics, and roundtrip their changes back into auditable deliverables.

## Core Idea

- **Universal extraction** – capture both narrative text and embedded tables from pharmaceutical PDF reports.
- **Editable representations** – emit markdown and HTML so reviewers can iterate inside modern editors (e.g. tiptap) without touching the raw PDF.
- **Structured data access** – serialise tables to CSV/JSON and create Python-friendly data frames for downstream processing.
- **Data-quality guardrails** – automatically validate extracted metadata to highlight integrity, consistency, and completeness issues before analysts make decisions.

## Methodology & Workflow

1. **Ingest**
   - Text extraction relies on `pdfminer.six`, with OCR fallback hooks for image-heavy pages.
   - Table detection supports multiple engines (`pdfplumber`, `camelot`, `tabula`) so we can switch strategies based on document layout.
2. **Transform**
   - Combine page text and table previews into a structured markdown narrative.
   - Build a parallel HTML document styled for editor-friendly rendering (the output loads cleanly in tiptap or similar WYSIWYG components).
3. **Persist**
   - Store markdown, HTML, CSV, and JSON artefacts alongside the original PDF.
   - Preserve edited markdown drafts by regenerating a `*.modified.pdf` snapshot before overwriting extractions.
4. **Validate**
   - Inspect tables for heuristics such as generic headers, blank cells, and inconsistent page counts.
   - Extract key/value metadata (dates, identifiers) and cross-check them against expectations derived from the file name and declaration text.
   - Emit both JSON and markdown quality reports so teams can automate checks or read-friendly summaries.

## Reasoning Behind the Approach

- **Analyst-first editing** – markdown/HTML outputs mirror the original page order, keeping context intact while enabling inline edits.
- **Redundancy in table engines** – different reports vary wildly; supporting multiple extraction strategies improves recall.
- **Lightweight QA** – automated checks catch obvious anomalies (e.g. mismatched report number, malformed dates) before deeper analytics, reducing manual triage time.
- **Roundtrip fidelity** – saving modified drafts to separate PDFs honours regulatory requirements to keep originals untouched while capturing revision history.

## Current Capabilities

- CLI command: `poetry run python -m pdf_analysis.cli extract <PDF> -o <outdir> [--engine] [--ocr-fallback] [--max-pages]`
- Outputs:
  - `*.extracted.md` – markdown with interleaved tables.
  - `*.extracted.html` – HTML suitable for rich-text editors.
  - `*.pX.tY.csv/json` – machine-readable tables.
  - `*.quality.json/md` – data-quality diagnostics.
  - Optional `*.modified.pdf` if the analyst previously edited the markdown draft.
- API service: `poetry run uvicorn pdf_analysis.api.server:app --reload`
  - `POST /analyze` accepts JSON `{ "bucket": "...", "key": "...", "version_id": "..."? }`, streams the PDF from S3, auto-selects the best table extraction engine/OCR strategy, and returns the markdown plus generated metadata (the full analysis payload is included under `analysis`).
  - `POST /s3/upload-analyze` accepts the PDF, S3 destination info (`bucket`, `company`, `project`, optional `folder`), pushes the object to S3, runs extraction/labeling, and returns either the full analysis (when it finishes within `wait_timeout_seconds`, default 25s) or a `202 Accepted` payload with polling links.
    - Synchronous responses include the extracted markdown, generated metadata, and the S3 identifiers for both the PDF and the stored `.analysis.json` artefact.
    - Asynchronous responses always persist artefacts to S3 (the PDF, `<key>.meta.json`, `<key>.analysis.json`). The payload surfaces `status_url` and `result_url` helpers:
      - `GET /s3/analysis/status?bucket=...&key=...` → reports `pending` or `completed`.
      - `GET /s3/analysis/result?bucket=...&key=...` → returns the stored analysis JSON once ready (404 while pending).
    - Front-ends can poll the status endpoint, watch for S3 events (SNS/SQS), or subscribe to your own notification channel triggered off the `.analysis.json` upload.
  - `POST /s3/markdown` downloads the PDF from S3, re-runs extraction, and returns the markdown together with the S3 key metadata so clients always see where the content originated.

## Next Steps

- Expand validation rules with document-specific business logic (e.g. numeric ranges, cross-table comparison).
- Integrate an interactive notebook or light web UI to offer “playground” analytics directly on extracted tables.
- Implement PDF regeneration that merges edited markdown back into a polished report layout.
- Add automated regression tests using a curated corpus of sample PDFs to protect against extraction regressions.

## Deploying to AWS (ECR + ECS)

- Build the container locally with `docker build -t pdf-analysis:latest .`; the image exposes `PORT=8000` and serves the FastAPI app via Uvicorn.
- Push and deploy using `scripts/deploy_ecr_ecs.sh`; export `AWS_ACCOUNT_ID`, `AWS_REGION`, `ECS_CLUSTER`, `ECS_SERVICE`, and optionally override `ECR_REPOSITORY`, `IMAGE_TAG`, or `DOCKER_PLATFORM`.
- The script logs in to ECR, creates the repository if missing, builds/pushes the image, and triggers an ECS service rollout (`--force-new-deployment`).
- Ensure your ECS task definition maps container port `8000` to your load balancer target group or service discovery endpoint.
- Grant the execution role `AmazonECSTaskExecutionRolePolicy` and ECR read permissions so tasks can pull the image.

### GitHub Actions CI/CD

- A reusable workflow lives at `.github/workflows/deploy.yml`. It runs tests on every push/PR, builds and pushes the Docker image, and rolls the ECS service when changes land on `main` (or when manually triggered).
- Add the following GitHub Secrets before enabling the deployment job:
  - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
  - `AWS_ACCOUNT_ID`, `AWS_REGION`
  - `ECR_REPOSITORY` (e.g. `pdf-analysis`)
  - `ECS_CLUSTER`, `ECS_SERVICE`
  - `ECS_TASK_DEFINITION` (family or full ARN of the task definition to clone)
  - `ECS_CONTAINER_NAME` (container definition name to swap the image on)
- The job pulls the current task definition, swaps the container image to `<account>.dkr.ecr.<region>.amazonaws.com/<repository>:<git-sha>`, registers a new revision, and updates the service. It waits for the service to stabilise before finishing.
- Ensure the IAM user/role backing the access keys can call `ecr:*`, `ecs:DescribeTaskDefinition`, `ecs:RegisterTaskDefinition`, `ecs:UpdateService`, and `ecs:DescribeServices`.
- Adjust the workflow to skip the test stage or to install optional extras (OCR, LLM) if your deployment pipeline requires them.

### API Gateway Front Door

- A Terraform module in `infra/terraform/api-gateway` provisions an API Gateway HTTP API that proxies traffic through a VPC link to the ECS service’s load balancer.
- Supply the ALB listener ARN, subnet IDs, and security group IDs that allow connectivity from the VPC link to the ECS target.
- Enable access logs (default) or configure a custom CloudWatch log group/format via module variables.
- If you maintain a custom domain, create it separately and pass the name via `custom_domain_name`; the module will create the stage mapping.
- See `infra/terraform/api-gateway/README.md` for a complete variable reference and an example usage block.

## Adaptive Extraction Pipeline

- The orchestrator in `pdf_analysis.pipeline` runs text extraction, OCR, structured parsing, and optional LangChain post-processing with graceful fallbacks.
- Configure stages via `PipelineConfig`—pick engines for pure text (`pdfplumber`, `pymupdf`, `pdfminer`), choose OCR providers (`tesseract`, `textract`, `gcv`), and supply regexes or SDK callables for form/table extraction.
- Enable LLM summarisation by installing the `llm` extra and toggling `LangChainConfig(enabled=True, provider="openai", model="gpt-4o-mini")`; the pipeline will build prompts and invoke the configured chain.
- Optional extras: `poetry install --with ocr,pymupdf,llm` (add `cloud_ocr` when wiring Google Vision or Textract clients). OCR fallbacks that use pdf2image expect the Poppler binaries (`pdfinfo`, `pdftoppm`) on `PATH`.
- Parallelism: batch PDFs with `PipelineRunner` to fan out work across threads while keeping per-PDF state isolated.
- API workers: set `PDF_PIPELINE_MAX_WORKERS=<int>` to bound the thread pool each request uses when the FastAPI endpoint fans out work.
- Diagnostics: every run emits `PipelineMetrics` (text/table coverage, OCR usage, heuristic confidence) and embeds the headline figures inside the quality report payload.
- Chunked streaming: iterate with `PDFProcessingPipeline.stream(...)` to receive `PipelineChunk` objects one batch of pages at a time. Each chunk includes text, tables, OCR flags, and can be persisted to Redis by passing `redis_client`/`redis_key`, enabling long-running docs to stream straight into downstream consumers.
- Logging: configure via `logging.basicConfig(level=logging.INFO)` (or DEBUG) before constructing the pipeline; the module logs each stage’s progress under `pdf_analysis.pipeline`.
- Quick start:

  ```python
  from pathlib import Path
  from pdf_analysis.pipeline import (
      PDFProcessingPipeline,
      PipelineConfig,
      TextExtractionConfig,
      OCRConfig,
      StructuredExtractionConfig,
      LangChainConfig,
      PipelineRunner,
  )

  config = PipelineConfig(
      text=TextExtractionConfig(engines=("pdfplumber", "pymupdf", "pdfminer"), keep_intermediate=True),
      ocr=OCRConfig(enable=True, strategy="tesseract", languages="eng"),
      structured=StructuredExtractionConfig(
          table_engines=("pdfplumber", "camelot"),
          key_value_patterns={"protocol_id": r"Protocol\s*#?\s*(\\w+)"},
      ),
      llm=LangChainConfig(enabled=False),
  )

  result = PDFProcessingPipeline(config=config).run(Path("./sample.pdf"))
  print(result.text_engine, len(result.pages), result.metrics.confidence)

  runner = PipelineRunner(config=config, max_workers=4)
  batch = runner.run_many([Path("./sample.pdf"), Path("./other.pdf")])
  print([task.result.metrics.text_coverage if task.result else None for task in batch])
  ```

- Streaming usage example:

  ```python
  from pathlib import Path
  from redis import Redis
  from pdf_analysis.pipeline import PDFProcessingPipeline

  redis_client = Redis.from_url("redis://localhost:6379/0")
  pipeline = PDFProcessingPipeline()

  for chunk in pipeline.stream(
      Path("./large.pdf"),
      chunk_size=5,
      redis_client=redis_client,
      redis_key="doc:large",
      redis_expire=3600,
  ):
      print(f"chunk {chunk.chunk_index}: pages {[p['page_number'] for p in chunk.pages]}")
      # Optionally perform additional per-chunk post-processing here.
  ```

- Environment-specific Redis wiring can be centralised with `RedisStreamingConfig`:

  ```python
  from redis import Redis
  from pdf_analysis.pipeline import PipelineConfig, RedisStreamingConfig

  config = PipelineConfig(
      redis=RedisStreamingConfig(
          enabled=True,
          url="redis://dev-cache:6379/2",
          key_template="pdf:{stem}:{env}",
          expire_seconds=3600,
          client_factory=lambda cfg: Redis.from_url(cfg.url.replace("{env}", "dev")),
      )
  )

  pipeline = PDFProcessingPipeline(config=config)
  for _chunk in pipeline.stream(Path("./study.pdf"), chunk_size=10):
      pass  # chunks are persisted automatically using the derived key.
  ```

## S3 → Redis Synchronisation Service

- Install the infra extras to pick up the required clients:

  ```shell
  poetry install --with infra
  ```

- Configure a daily cron (or GitHub Action) that runs the new helper script:

  ```shell
  poetry run python scripts/s3_sync.py \
    --bucket YOUR_BUCKET \
    --company filynai.com \
    --projects LT1009 \
    --modules 1,2,3 \
    --redis-url redis://localhost:6379/0 \
    --redis-key-template "{company}:{project_slug}:{module_slug}:{filename}" \
    --redis-expire 86400 \
    --ai-metadata \
    --output-dir ./synced-markdown
  ```

  The script honours `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, and `AWS_REGION` environment variables, so credentials can be injected via your secrets manager instead of CLI flags.
  On startup it automatically loads variables from `.env` and `.env.local` (if present) using `python-dotenv`.
  When `--output-dir` is set, the service mirrors the S3 hierarchy locally, writing `<output>/<company>/<Project>/<Module N.*>/<filename>.pdf.md` and a companion `<filename>.pdf.meta.json` alongside the source PDF structure.
  Add `--ai-metadata` (and set `OPENAI_API_KEY`) to label each document via OpenAI, generating up to three labels and five keywords. The results are written back to Redis, uploaded to the bucket via `copy_object` as object metadata, and stored in the `.meta.json` artefact.
  The path parser expects keys shaped like `filynai.com/<Project>/Module N.<description>/...`. Module numbers are inferred automatically; pass `--modules` with integers (e.g. `--modules 1,2`) if you want to limit the scrape, otherwise omit the flag to index every module it encounters.

- Keys in Redis take the form `company:module:filename` and a hash payload with `markdown`, `s3_version`, `last_modified`, etc. The S3 document hierarchy is preserved, which makes it easy for downstream systems to correlate entries back to their source objects.

### Local Redis Quickstart

- macOS (Homebrew):

  ```shell
  brew install redis
  brew services start redis   # or `redis-server` to run in the foreground
  redis-cli ping              # should reply with PONG
  ```

- Ubuntu/Debian:

  ```shell
  sudo apt-get update
  sudo apt-get install redis-server
  sudo systemctl enable --now redis-server
  redis-cli ping
  ```

- Windows (WSL or native): install from <https://github.com/microsoftarchive/redis/releases> or run `sudo apt-get install redis-server` inside WSL. Ensure the daemon is listening on `localhost:6379`.

Point `REDIS_URL` at your local instance (e.g. `redis://localhost:6379/0`) and run the sync script. Use `redis-cli hgetall filynai.com:module1:example.pdf` to inspect the stored markdown.

### On-Demand S3 Markdown API

- Call the API endpoint to transform a single S3 object into markdown without running the full sync:

  ```http
  POST /s3/markdown
  Content-Type: application/json

  {
    "bucket": "YOUR_BUCKET",
    "key": "filynai.com/LT1009/Module 1.Quality/report.pdf",
    "version_id": "optional-version",
    "aws_region": "us-east-1"
  }
  ```

  The service downloads the object (using the environment AWS credentials), runs the pipeline, derives labels/keywords when `OPENAI_API_KEY` is present, and returns `{ "markdown": "...", "text_engine": "...", "tables": <count>, "metadata": {...} }`. Install the `infra` extras so boto3 and OpenAI are available on the API host.

- To upload edited markdown back to S3 (creating a new object version) send:

  ```http
  POST /s3/markdown/save
  Content-Type: application/json

  {
    "bucket": "YOUR_BUCKET",
    "path": "filynai.com/LT1009/Module 1.Quality",
    "filename": "report",
    "markdown": "# revised ...",
    "label": "annotated",
    "tags": {"status": "review", "reviewer": "QA"},
    "metadata": {"source": "editor"}
  }
  ```

  The service writes `<path>/<filename>.md` with `text/markdown`, attaches the `metadata` map to the object, persists tags, and uploads a companion `<path>/<filename>.md.meta.json` describing the revision. When you hit `POST /s3/markdown`, the pipeline performs the same metadata enrichment (labels, keywords, language) before returning the markdown payload and S3 reference. For richer querying (search, audit), mirror the JSON metadata into DynamoDB or another durable store.

### Generate OpenAPI Schema

- Produce a static OpenAPI document for integration or documentation tooling:

  ```shell
  poetry run python scripts/generate_openapi.py --output dist/openapi.json
  ```

  Adjust `--output` or `--indent` as needed. The script loads the FastAPI app defined in `pdf_analysis.api.server`, so ensure optional dependencies (e.g., boto3, openai) are available if you’ve enabled related routes.

## Build & Test Helper

- Use the helper script to install dependencies, run static checks, and execute tests in one shot:

  ```shell
  poetry run python scripts/build.py --with infra
  ```

  Optional flags:
  - `--with infra,llm` installs additional poetry groups.
  - `--skip-install` or `--skip-tests` bypass individual stages for faster iteration.
  - The script attempts `mypy` first (warnings are logged but do not stop the build) and finishes with `pytest` unless skipped.

- run test

  ```shell
  poetry run python scripts/pipeline.py data/42-stud-rep/421-pharmacol/4211-prim-pd/lt3114-pha-001-r/lt3114-pha-001-r.pdf
  poetry run python scripts/s3_sync.py --bucket doc-repository-dev  --company filynai.com --projects LT1009 --modules 1,2 --redis-url redis://localhost:6379/0 --ai-metadata --ai-embeddings
  poetry run python scripts/s3_sync.py --bucket doc-repository-dev  --company filynai.com --projects LT1009 --modules 1,2 --redis-url redis://localhost:6379/0 --ai-metadata --ai-embeddings
 --force
  ```
