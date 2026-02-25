# Template Summary Workflow

## Goal
Generate CTD 2.4/2.6 summaries that conform to the project template and section mapping while preserving source traceability.

## Step 1: Resolve Section Context
- Run:
```bash
python skills/ind-expert/scripts/ind_context_lookup.py --section 2.4.2 --element 2.4.2-b
```
- Read from output:
  - `template_matches`
  - `module4_mapping_matches`
  - `section_tree_matches`
  - `warnings`

## Step 2: Build Evidence Pack
- Collect source evidence by Module 4 section and study id.
- Use DB-backed traces where available (`source_chunk_id`, section spans, page ranges).
- Keep units as-written; normalize only with explicit conversion notes.

## Step 3: Draft Section Content
- Structure each section with:
  - Objective/context sentence.
  - Evidence synthesis paragraph(s).
  - Safety/translation interpretation.
  - Gaps/limitations.
- Keep claims specific and data-backed.

## Step 4: Attach Traceability
- Provide `evidence_map` entries:
  - `module4_section`
  - `study_id` or source document
  - `source_anchor` (chunk, page, section span, or table reference)
  - `claim`

## Step 5: Validate Before Finalizing
- Check required fields implied by the template entry.
- Check expected PK/tox parameters are present when section scope requires them.
- Mark unresolved items in `data_gaps` rather than inferring values.

## Suggested Output JSON Shape
```json
{
  "section": "2.4.2",
  "element": "2.4.2-b",
  "summary": "Concise regulator-facing synthesis...",
  "critical_claims": [
    "Claim 1",
    "Claim 2"
  ],
  "evidence_map": [
    {
      "claim": "Claim 1",
      "module4_section": "4.2.1.1",
      "study_id": "study-123",
      "source_anchor": "chunk:... page:..."
    }
  ],
  "data_gaps": [
    "Missing repeat-dose exposure margin in non-rodent species"
  ]
}
```
