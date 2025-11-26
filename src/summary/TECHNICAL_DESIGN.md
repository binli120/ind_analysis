# IND 2.4 Auto-Generation – Technical Design

## Goal
Automate IND 2.4 (Nonclinical Overview) generation from Section 2.6 source content stored in S3/Redis, including gap detection and auto-drafting of missing sections. Outputs are Markdown (human-readable) plus JSON where needed.

## High-Level Flow
1) **Collect Section 2.6 content**
   - Auto-discover S3 prefix containing “2.6” under `<company>/<project>` (or use provided prefix).
   - Pull `.md` sidecars directly; if missing, fetch PDF and generate markdown via `PDFProcessingPipeline`.
   - Optional Redis lookup for cached markdown (`redis_key_template` aligned with s3_sync).
2) **Chunking & summarization (map phase)**
   - `_chunk_markdown` splits markdown by headings with token/segment limits (`max_gap_tokens_per_chunk`, `max_chunks`), overflowing into the last chunk to avoid loss.
   - `summarize_chunks` produces concise per-chunk summaries (OpenAI chat).
3) **Gap analysis (map + merge)**
   - `run_gap_analysis_chunked` runs OpenAI gap analysis per chunk with timeouts.
   - `_merge_gap_structured` + `_validate_gap_structured` dedupe/normalize missing & incomplete sections.
4) **Auto-generate missing sections**
   - If gaps exist, `generate_missing_sections` asks OpenAI to draft short prose (or placeholders) for each missing item using condensed context.
   - Stored as `auto_generated_sections` in gap results.
5) **2.4 assembly (reduce phase)**
   - `_build_condensed_summary_input` joins chunk summaries.
   - `generate_summary` feeds condensed input + gap payload into `ind_2_4_generation_template.json` to get final 2.4 JSON, then rendered to Markdown.
6) **Persist outputs**
   - `section_2_6_combined.md`
   - `section_2_6_gap_analysis.md` (text only, includes auto-generated drafts)
   - `section_2_6_gap_analysis.json` (structured payload + auto-generated sections)
   - `section_2_4_summary.md`

## Key Components
- `IND24GenerationConfig`
  - Chunk controls: `max_chunks` (default 12), `max_gap_tokens_per_chunk`, `max_summary_tokens_per_chunk`
  - Timeouts: `llm_timeout` (default 120s)
  - Output keys: `output_gap_key`, `output_gap_json_key`, `output_summary_key`, `output_combined_markdown_key`
- `Section26MarkdownCollector`
  - S3 + Redis fetch; PDF-to-markdown fallback; optional write-back of generated `.md`.
- `IND24LLMClient`
  - Chunk summaries, chunked gap analysis, missing-section auto-generation, final 2.4 generation.
- Formatting helpers
  - Markdown renderers for gap/summary; JSON persisted separately.

## Error/Limit Handling
- Token-aware chunking with max chunk count; overflow appended to last chunk.
- Per-call timeouts for OpenAI; logged durations.
- Truncation safeguards (`max_gap_chars`, `max_summary_chars`) before LLM calls.
- Dedupe/validate gap outputs to avoid repeated entries.

## Configuration / Env
- Required: `OPENAI_API_KEY`, AWS creds (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`), bucket name.
- Optional: `REDIS_URL`, `SECTION_PREFIX`, `MAX_CHUNKS`, `MAX_GAP_TOKENS_PER_CHUNK`, `MAX_SUMMARY_TOKENS_PER_CHUNK`, `LLM_TIMEOUT`.

## Outputs & Paths
- Auto-discovered or provided Section 2.6 prefix; outputs saved alongside:
  - `section_2_6_combined.md`
  - `section_2_6_gap_analysis.md`
  - `section_2_6_gap_analysis.json`
  - `section_2_4_summary.md`

## Run Workflow (CLI)
```
poetry run python scripts/generate_ind24.py \
  --bucket <bucket> \
  --company filynai.com \
  --project LT1009 \
  --redis-url redis://localhost:6379/0 \
  --log-level INFO
```
Flags: `--section-prefix` (optional override), `--max-chunks`, `--log-level DEBUG` for troubleshooting.
