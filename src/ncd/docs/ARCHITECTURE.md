# Architecture


# **IND AI Platform – System Architecture Overview**

This document describes the full architecture for the IND Document Intelligence Platform, which ingests nonclinical PDFs, extracts structured scientific data, classifies study types, builds a dynamic nonclinical dictionary, and auto-generates CTD Module 2.4 and 2.6 summaries.

---

# **1. High-Level Architecture**

<pre class="overflow-visible!" data-start="586" data-end="2553"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>                           +</span><span>---------------------------+</span><span>
                           |     </span><span>User</span><span> / Frontend       |
                           |  (Next.js, React, SaaS)   |
                           +</span><span>-------------+-------------+</span><span>
                                         |
                                         v
                       +</span><span>-----------------+-----------------+</span><span>
                       |      API Gateway / Backend        |
                       |     (FastAPI </span><span>or</span><span> Flask + Uvicorn)  |
                       +</span><span>-----------------+-----------------+</span><span>
                                         |
                                         v
           +</span><span>-----------------------------+------------------------------+</span><span>
           |                           Services                         |
           +</span><span>-----------------------------+------------------------------+</span><span>
           | </span><span>1.</span><span> PDF Ingestion Service                                   |
           | </span><span>2.</span><span> Study Classification Service (LLM)                      |
           | </span><span>3.</span><span> Data Extraction Service (LLM + Patterns + </span><span>Tables</span><span>)       |
           | </span><span>4.</span><span> Dynamic </span><span>Dictionary</span><span> Builder                              |
           | </span><span>5.</span><span> CTD </span><span>2.4</span><span> / </span><span>2.6</span><span></span><span>Summary</span><span> Generator                         |
           +</span><span>-----------------------------+------------------------------+</span><span>
                                         |
                                         v
                     +</span><span>-------------------+-------------------+</span><span>
                     |      PostgreSQL / Supabase           |
                     |  tenant-aware, program-aware </span><span>storage</span><span> |
                     +</span><span>-------------------+-------------------+</span><span>
                                         |
                                         v
                           +</span><span>-------------+-------------+</span><span>
                           |      Vector Store         |
                           |      (ChromaDB/pgvector)  |
                           +</span><span>---------------------------+</span><span>
</span></span></code></div></div></pre>

---

# **2. Core Data Flow**

<pre class="overflow-visible!" data-start="2585" data-end="2762"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span></span><span>PDF</span><span></span><span>Upload</span><span> → </span><span>PDF</span><span></span><span>Parser</span><span> → </span><span>LLM</span><span></span><span>Classification</span><span> → </span><span>LLM</span><span></span><span>Extraction</span><span> →  
       </span><span>Dynamic</span><span></span><span>Dictionary</span><span></span><span>Update</span><span> → </span><span>Vector</span><span></span><span>DB</span><span></span><span>Index</span><span> →  
       </span><span>Query</span><span></span><span>Engine</span><span> → </span><span>CTD</span><span></span><span>2.4</span><span></span><span>/</span><span></span><span>2.6</span><span></span><span>Generator</span><span>
</span></span></code></div></div></pre>

### Step-by-step:

1. **Upload PDFs**

   User uploads raw toxicology, PK, safety pharm, and analytical study files.
2. **PDF Parsing Layer**

   * Extract text
   * Extract tables to structured rows
   * Extract images and captions
   * Extract headings like **4.2.1.1** automatically
3. **Study Type Classification (LLM)**

   * Toxicology → repeat-dose, single-dose, safety, pathology
   * PK → rat, monkey, single-dose, repeat-dose
   * Safety pharm
   * Analytical / formulation stability
   * TK, ADA, bioanalysis
4. **Data Extraction Layer**

   Automatically extract:

   * NOAEL
   * Cmax, AUC, Tmax
   * Dose groups, mg/kg
   * Findings per organ
   * TK tables
   * Materials & methods metadata
   * Study dates
   * Species, n, strain
   * Deaths, morbidity
5. **Dynamic Dictionary Auto-Generation**

   Dictionary entries include:

   * standardized CTD section mapping
   * topic anchors (e.g., "NOAEL", "TK Results")
   * searchable key-value entries
   * canonical wording for summaries
   * citations referencing exact PDF segments
6. **Vector Indexing**

   All extracted passages + dictionary entries become searchable embeddings.
7. **Query Engine**

   * "What is the NOAEL for rat studies?"
   * "Show findings related to kidney toxicity"
   * "Show all PK Cmax tables for cynomolgus monkeys"
8. **Auto-Generation of CTD 2.4 / 2.6**

   * Fills templates
   * Pulls dictionary values
   * Uses LLM to synthesize narratives
   * Includes citations (with `<refX>` tags)
   * Produces DOCX/PDF output

---

# **3. Backend Services**

## **3.1 PDF Ingestion Service**

* Extract text, tables, images
* Assign document UUID
* Trigger ingestion pipeline
* Store parsed text into:
  * `raw_text_chunks`
  * `tables`
  * `figures`
  * `section_headings`

## **3.2 Classification Service**

Powered by LLM + regex assist.

Determines:

* study type
* species
* route of administration
* GLP vs non-GLP
* study duration
* TK-only vs safety vs toxicology

## **3.3 Extraction Service**

LLM + rule-based hybrid system.

Extracts:

* key toxicology outcomes
* NOAEL logic
* dose-response findings
* TK table values
* PK parameters
* organ-specific findings
* clinical chemistry/hematology summaries

## **3.4 Dynamic Dictionary Service**

Dictionary is multi-layered:

<pre class="overflow-visible!" data-start="5148" data-end="5264"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Tenant </span><span>Dictionary</span><span>
    ↳ Program </span><span>Dictionary</span><span>
         ↳ Document </span><span>Dictionary</span><span>
              ↳ Extraction Results
</span></span></code></div></div></pre>

Each node stores:

* topic label
* section number (e.g., 4.2.1.1)
* synonyms
* embeddings
* confidence scores
* references

## **3.5 CTD Summary Generator**

Generates:

* **Module 2.4 Nonclinical Overview**
* **Module 2.6 Nonclinical Summaries**
  * 2.6.1 PK
  * 2.6.2 Pharmacology
  * 2.6.3 Toxicology

Uses:

* dictionary entries
* extracted tables
* LLM summarization
* template mapping rules

Outputs:

* DOCX, PDF, or JSON

---

# **4. Storage Architecture**

## **4.1 PostgreSQL Schema Layers**

<pre class="overflow-visible!" data-start="5792" data-end="5954"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>tenant
user
program
document
document_version
extraction_result
dictionary_entry
dictionary_topic
dictionary_link
ctd_mapping_rule
</span><span>search_index</span><span></span><span>(optional)</span><span>
</span></span></code></div></div></pre>

## **4.2 Vector DB (pgvector or ChromaDB)**

Stores:

* embeddings for chunks
* embeddings for dictionary entries
* embeddings for extracted tables

Used for:

* semantic search
* topic clustering
* mapping Module 4 → Module 2

---

# **5. Ingestion Pipeline (Event-Driven)**

<pre class="overflow-visible!" data-start="6243" data-end="6475"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>on</span><span>_pdf_</span><span>uploaded
    → parse_pdf
    → classify_document
    → extract_metadata
    → extract_structured_values
    → extract_tables
    → update_dictionary
    → update_vector_index
    → notify_frontend (ingestion_complete)
</span></span></code></div></div></pre>

Each step is modular so they can run in Celery, AWS Lambda, or ECS tasks.

---

# **6. CTD Mapping Rules Engine**

Maps automatically:

<pre class="overflow-visible!" data-start="6613" data-end="6760"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Toxicology → CTD </span><span>2.6</span><span>.</span><span>6</span><span>, </span><span>2.6</span><span>.</span><span>7</span><span>, </span><span>2.6</span><span>.</span><span>8</span><span>
PK → CTD </span><span>2.6</span><span>.</span><span>1</span><span>
Safety pharmacology → CTD </span><span>2.6</span><span>.</span><span>3</span><span>
TK → Attach </span><span>to</span><span> PK </span><span>section</span><span>
ADA → Attach </span><span>to</span><span> TK/PK </span><span>section</span><span>
</span></span></code></div></div></pre>

Rules stored in DB:

* pattern-based
* semantic similarity threshold
* override options at tenant/program level

---

# **7. API Layer**

Endpoints:

<pre class="overflow-visible!" data-start="6917" data-end="7065"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>POST /upload
POST /ingest/{document_id}
</span><span>GET</span><span> /</span><span>dictionary</span><span>/topics
</span><span>GET</span><span> /</span><span>dictionary</span><span>/</span><span>search</span><span>?q=
</span><span>GET</span><span> /ctd/</span><span>2.4</span><span>/{program_id}
</span><span>GET</span><span> /ctd/</span><span>2.6</span><span>/{program_id}
</span></span></code></div></div></pre>

Response formats:

`JSON`, `DOCX`, `HTML`, `Markdown`.

---

# **8. Frontend Architecture**

* React / Next.js
* Multi-tenant awareness
* Progress indicators for ingestion
* Sidebar dictionary explorer
* CTD preview panel
* Inline citations
* AI editable summaries

---

# **9. Security Model**

* tenancy enforced via RLS
* document separation per program
* audit logs
* versioning of dictionary entries
* role-based access:
  * viewer
  * scientist
  * reviewer
  * admin

---

# **10. Deploy Architecture**

<pre class="overflow-visible!" data-start="7611" data-end="7752"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>AWS ECS → containerized services
AWS S3 → PDF </span><span>storage</span><span>
Supabase → Postgres + RLS
ChromaDB </span><span>or</span><span> pgvector
OpenAI API / </span><span>Local</span><span> Llama </span><span>for</span><span> LLM
</span></span></code></div></div></pre>

Autoscaling optional:

* ingestion tasks
* extraction workers
* summary generation workers

---
