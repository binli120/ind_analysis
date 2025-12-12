# Database Schema

# Database Schema

IND PDF Analysis — Data Model Specification
Version 1.0

This document defines the relational schema for the IND PDF Analysis platform.It is engineered to support:

- Multi-tenant architecture
- Study document ingestion
- PDF → page → chunk storage
- LLM-based classification and extraction
- Search index (pgvector)
- Dynamic dictionary
- CTD 2.4 / 2.6 mapping
- User-level and tenant-level configuration

The schema is optimized for **fast semantic search**, **traceability to source**, and **regulatory reproducibility**.

---

# 1. ERD (ASCII Diagram)

+-------------------+ +------------------------+

| tenants | 1 n | users |

+-------------------+---------+------------------------+

| id (PK) | | id (PK) |

| name | | tenant_id (FK) |

| created_at | | email |

+-------------------+ | role |

+------------------------+

+-------------------------+ +-----------------------------+

| projects | 1 | source_documents |

+-------------------------+----+-----------------------------+

| id (PK) | | id (PK) |

| tenant_id (FK) | | project_id (FK) |

| name | | filename |

| created_at | | doc_type (pdf, docx) |

+-------------------------+ | study_type (predicted) |

| ingestion_status |

+-----------------------------+

+------------------------------+

| document_pages |

+------------------------------+

| id (PK) |

| source_document_id (FK) |

| page_number |

| text_content |

+------------------------------+

+------------------------------+

| text_chunks |

+------------------------------+

| id (PK) |

| page_id (FK) |

| chunk_index |

| content |

| section_number (nullable) |

| section_title (nullable) |

| key_topics (jsonb) |

+------------------------------+

+------------------------------+

| embeddings |

+------------------------------+

| id (PK) |

| chunk_id (FK) |

| vector (pgvector) |

+------------------------------+

+------------------------------+

| extraction_results |

+------------------------------+

| id (PK) |

| chunk_id (FK) |

| extractor_name |

| result_json (jsonb) |

| confidence |

+------------------------------+

+------------------------------+

| studies |

+------------------------------+

| id (PK) |

| source_document_id (FK) |

| study_type (llm_classified) |

| species |

| route |

| duration |

| design_json (jsonb) |

+------------------------------+

+------------------------------+

| ctd_mappings |

+------------------------------+

| id (PK) |

| study_id (FK) |

| ctd_section (e.g., 2.6.6.3) |

| generated_summary (text) |

| evidence_chunks (jsonb) |

+------------------------------+

+------------------------------+

| user_configurations |

+------------------------------+

| id (PK) |

| user_id (FK) |

| temperature |

| system_prompt |

| extraction_prompt_overrides |

| ctd_prompt_overrides |

+------------------------------+

+------------------------------+

| tenant_configurations |

+------------------------------+

| id (PK) |

| tenant_id (FK) |

| default_prompts (jsonb) |

| dictionary_terms (jsonb) |

+------------------------------+

---

# 2. Tables & Columns (Detailed Specification)

---

## 2.1 **tenants**

| Column     | Type        | Notes               |
| ---------- | ----------- | ------------------- |
| id (PK)    | UUID        | Primary key         |
| name       | text        | Tenant/company name |
| created_at | timestamptz |                     |

---

## 2.2 **users**

| Column     | Type        | Notes                        |
| ---------- | ----------- | ---------------------------- |
| id (PK)    | UUID        |                              |
| tenant_id  | UUID (FK)   | References `tenants.id`      |
| email      | text        | Unique                       |
| role       | text        | admin / scientist / reviewer |
| created_at | timestamptz |                              |

---

## 2.3 **projects**

| Column     | Type        | Notes              |
| ---------- | ----------- | ------------------ |
| id (PK)    | UUID        |                    |
| tenant_id  | UUID (FK)   |                    |
| name       | text        | IND program folder |
| created_at | timestamptz |                    |

---

## 2.4 **source_documents**

Stores each uploaded PDF or DOCX.

| Column           | Type  | Notes                                           |
| ---------------- | ----- | ----------------------------------------------- |
| id (PK)          | UUID  |                                                 |
| project_id (FK)  | UUID  |                                                 |
| filename         | text  |                                                 |
| doc_type         | text  | pdf / docx                                      |
| study_type       | text  | GLP Repeat-dose / Single Dose PK / Safety Pharm |
| ingestion_status | text  | pending / extracted / classified / complete     |
| metadata_json    | jsonb | optional (pages, size, etc.)                    |

---

## 2.5 **document_pages**

| Column             | Type      | Notes    |
| ------------------ | --------- | -------- |
| id (PK)            | UUID      |          |
| source_document_id | UUID (FK) |          |
| page_number        | integer   |          |
| text_content       | text      | OCR text |

---

## 2.6 **text_chunks**

Chunks are used for embedding, classification, and extraction.

| Column         | Type  | Notes                                   |
| -------------- | ----- | --------------------------------------- |
| id (PK)        | UUID  |                                         |
| page_id (FK)   | UUID  |                                         |
| chunk_index    | int   |                                         |
| content        | text  |                                         |
| section_number | text  | nullable, auto-detected (e.g., 4.2.3.1) |
| section_title  | text  | extracted header                        |
| key_topics     | jsonb | list of detected topics                 |

---

## 2.7 **embeddings**

| Column   | Type      | Notes              |
| -------- | --------- | ------------------ |
| id (PK)  | UUID      |                    |
| chunk_id | UUID (FK) |                    |
| vector   | vector    | pgvector embedding |

Index suggestion:

```sql
CREATE INDEX ON embeddings USING ivfflat (vector vector_cosine_ops);
```

## 2.8 **extraction_results**

| Column         | Type  | Notes                          |
| -------------- | ----- | ------------------------------ |
| id (PK)        | UUID  |                                |
| chunk_id (FK)  | UUID  |                                |
| extractor_name | text  | e.g., "noael", "pk_parameters" |
| result_json    | jsonb | structured fields              |
| confidence     | float |                                |

---

## 2.9 **studies**

Aggregated study-level metadata.

| Column             | Type      | Notes         |
| ------------------ | --------- | ------------- |
| id (PK)            | UUID      |               |
| source_document_id | UUID (FK) |               |
| study_type         | text      | LLM predicted |
| species            | text      |               |
| route              | text      |               |
| duration           | text      |               |
| design_json        | jsonb     |               |

---

## 2.10 **ctd_mappings**

Where extracted data is transformed into CTD narrative.

| Column            | Type  | Notes             |
| ----------------- | ----- | ----------------- |
| id (PK)           | UUID  |                   |
| study_id (FK)     | UUID  |                   |
| ctd_section       | text  | e.g.`2.6.6.3`     |
| generated_summary | text  | LLM output        |
| evidence_chunks   | jsonb | list of chunk IDs |

---

## 2.11 **user_configurations**

User-level overrides.

| Column                      | Type  |
| --------------------------- | ----- |
| id (PK)                     | UUID  |
| user_id (FK)                | UUID  |
| temperature                 | float |
| system_prompt               | text  |
| extraction_prompt_overrides | jsonb |
| ctd_prompt_overrides        | jsonb |

---

## 2.12 **tenant_configurations**

Tenant-level defaults.

| Column           | Type  |
| ---------------- | ----- |
| id (PK)          | UUID  |
| tenant_id (FK)   | UUID  |
| default_prompts  | jsonb |
| dictionary_terms | jsonb |

---

# 3. Dynamic Nonclinical Dictionary

The dictionary stores:

- Toxicology terminology
- Organ system keywords
- Findings (e.g., hepatocellular hypertrophy)
- Dose-related patterns
- PK terms
- Safety pharmacology endpoints
- CTD mapping logic

Example:

<pre class="overflow-visible!" data-start="9073" data-end="9228"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"hepatic"</span><span>:</span><span></span><span>[</span><span>"ALT"</span><span>,</span><span></span><span>"AST"</span><span>,</span><span></span><span>"bilirubin"</span><span>,</span><span></span><span>"centrilobular"</span><span>]</span><span>,</span><span>
  </span><span>"kidney"</span><span>:</span><span></span><span>[</span><span>"BUN"</span><span>,</span><span></span><span>"creatinine"</span><span>,</span><span></span><span>"glomerulus"</span><span>]</span><span>,</span><span>
  </span><span>"pk"</span><span>:</span><span></span><span>[</span><span>"Cmax"</span><span>,</span><span></span><span>"AUC"</span><span>,</span><span></span><span>"t1/2"</span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

Stored inside:

<pre class="overflow-visible!" data-start="9246" data-end="9292"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>tenant_configurations.dictionary_terms
</span></span></code></div></div></pre>

---

# 4. Searching the Database

Fast search is built on:

1. **Embedding search**
2. **Section number search**
3. **Topic search**
4. **Document → page → chunk traceability**

Example SQL:

<pre class="overflow-visible!" data-start="9486" data-end="9627"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-sql"><span><span>SELECT</span><span> c.id, c.content
</span><span>FROM</span><span> text_chunks c
</span><span>JOIN</span><span> embeddings e </span><span>ON</span><span> e.chunk_id </span><span>=</span><span> c.id
</span><span>ORDER</span><span></span><span>BY</span><span> e.vector </span><span><=></span><span> embedding(:query)
LIMIT </span><span>20</span><span>;
</span></span></code></div></div></pre>

---

# 5. Future Extensions

- Full audit trail for regulatory compliance
- Study-level QC flags
- Multi-model extraction
- Model version tracking
- Per-tenant dictionary training

---

# ✔ End of DATABASE_SCHEMA.md

## <pre class="overflow-visible!" data-start="9855" data-end="9929" data-is-last-node=""><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>

</span><span># ✅ **Next file?** </span><span>
</span><span>Reply:</span><span>

</span><span>**ARCHITECTURE.md**</span><span>  
</span><span>or</span><span>  
</span><span>**Next**</span></span></code></div></div></pre>
