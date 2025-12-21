# Section Summaries, Keywords, and Embeddings
Module 4 PDF Processing -> CTD 2.4 / 2.6 Generation

This document defines how to store and use section numbers, summaries, keywords,
and embeddings for Module 4 PDFs without duplicating large text blobs in the database.

Goals:
- Avoid storing full section text in Postgres
- Preserve traceability to original content
- Support fast search and CTD 2.4 / 2.6 generation
- Keep the system re-runnable and version-safe

---

## 1) Core Design Principles
- Store full text once in object storage (S3 or local file), not in DB.
- Store section boundaries (offsets, pages) in DB.
- Store semantic derivatives (summary, keywords, embeddings) in DB.
- Reconstruct section text on demand using offsets.

---

## 2) Data Flow Overview

PDF -> processed.md (object storage)
   |
   v
document_sections (offsets, section numbers)
   |
   +-> document_section_summary (summary + keywords)
   |
   +-> document_section_embedding (vector)

---

## 3) Full Text Storage (Single Source of Truth)
Full processed text lives in object storage and is referenced by the document version.

Example:
```
s3://<bucket>/<tenant>/<project>/<document_version_id>/processed.md
```

No section text is duplicated in the database.

---

## 4) Section Index Table (Structural Layer)
`document_sections` stores section location and metadata.

```sql
CREATE TABLE document_sections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_version_id UUID NOT NULL,

  section_number TEXT NOT NULL,      -- e.g. "4.2.1.1"
  section_title TEXT NULL,

  char_start INT NOT NULL,           -- offset in processed.md
  char_end INT NOT NULL,

  page_start INT NULL,
  page_end INT NULL,

  created_at TIMESTAMPTZ DEFAULT now(),

  UNIQUE (document_version_id, section_number)
);
```

This table is a structural index only. It never stores large text.

---

## 5) Section Summary + Keywords (Semantic Layer)
Summaries and keywords are small, derived outputs from LLMs and are stored in DB.

```sql
CREATE TABLE document_section_summary (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  section_id UUID NOT NULL
    REFERENCES document_sections(id) ON DELETE CASCADE,

  summary_type TEXT NOT NULL,         -- extractive | abstractive
  summary_purpose TEXT NOT NULL,      -- ctd_2_6 | ctd_2_4 | search | reviewer

  summary_text TEXT NOT NULL,

  keywords TEXT[] NOT NULL DEFAULT '{}',

  model_name TEXT,
  confidence FLOAT,

  created_at TIMESTAMPTZ DEFAULT now()
);
```

### Keywords Guidance
Use canonical, domain-specific terms. Examples:
- ["NOAEL", "repeat-dose toxicity", "liver", "ALT", "histopathology"]
- ["toxicokinetics", "Cmax", "AUC", "exposure margin"]
- ["safety pharmacology", "ECG", "QT interval"]

Avoid stop words, page numbers, and table labels.

---

## 6) Section Embeddings (Vector Layer)
Embeddings are stored per section and can be rebuilt any time.

```sql
CREATE TABLE document_section_embedding (
  section_id UUID PRIMARY KEY
    REFERENCES document_sections(id) ON DELETE CASCADE,

  embedding VECTOR(1536),
  model_name TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

---

## 7) How Section Text Is Retrieved (On Demand)
To retrieve original section content:
1. Load the processed markdown file for the document version.
2. Use `char_start` / `char_end` from `document_sections`.
3. Slice the string in memory.

Pseudo-code:
```python
text = load_processed_markdown(document_version_id)
section = text[char_start:char_end]
```

No text duplication is required.

---

## 8) CTD 2.4 / 2.6 Generation Usage
For CTD generation:
- Use `document_section_summary.summary_text` as the primary input.
- Use `keywords` to filter relevant sections.
- Pull raw section text only if needed (low confidence or quoting).

Example filter:
```sql
SELECT summary_text, keywords
FROM document_section_summary s
JOIN document_sections ds ON ds.id = s.section_id
WHERE s.summary_purpose = 'ctd_2_6'
  AND s.keywords && ARRAY['NOAEL', 'toxicity'];
```

---

## 9) LLM Prompt Pattern for Summary + Keywords
Use a single call to generate both summary and keywords:

```
Return JSON with:
{
  "summary": "<concise regulatory summary>",
  "keywords": ["keyword1", "keyword2", "..."]
}
```

Keyword rules:
- 5 to 10 keywords
- 1 to 3 words each
- normalized (singular form)
- domain-specific only

---

## 10) Why This Design Works
- No text duplication
- Fast search and CTD mapping
- Version-safe and re-runnable
- Traceable and auditable
- Scales to large programs

---

## 11) Recommendation Summary
- Store full processed text once in object storage.
- Store section offsets in `document_sections`.
- Store summaries + keywords in `document_section_summary`.
- Store embeddings in `document_section_embedding`.
- Reconstruct section text on demand.

This design delivers storage efficiency and regulatory-grade traceability.
