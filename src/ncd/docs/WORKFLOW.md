# Workflow

# **📘 IND AI Ingestion & CTD Summary Generation — End-to-End Workflow**

This document explains the **full operational pipeline** used to convert uploaded nonclinical study PDFs (Module 4) into **searchable structured data** , **LLM-extracted key parameters** , and **auto-generated CTD summaries** for **Module 2.4 (Nonclinical Overview)** and **Module 2.6 (Nonclinical Written Summary)** .

---

# **1. High-Level Architecture Overview**

### **Pipeline Components**

1. **Upload Service**
   - Receives PDFs (tox, PK, safety pharm, TK, antibody, histopathology, dose formulation, etc.).
   - Stores original files in S3/Supabase Storage.
2. **Document Ingestion Worker**
   - Converts PDF → text + images + tables.
   - Performs OCR when required.
   - Detects section hierarchy (3.x.x.x etc.).
   - Classifies the document (repeat-dose tox, TK, PK, safety pharm…).
   - Extracts metadata (study number, species, dose groups, schedule, route…).
3. **LLM Extractor Layer**
   - Extracts:
     - NOAEL values
     - Dose group tables
     - Toxicity findings
     - TK parameters (Cmax, AUC, t1/2)
     - PK parameters
     - Safety pharm endpoints
     - Histopathology summaries
   - Stores outputs in structured DB tables.
4. **Dynamic Nonclinical Dictionary**
   - Stores searchable keys:
     - Section numbers
     - Section titles
     - Detected topics
     - Entities (species, analytes, organs, endpoints, etc.)
     - Values (Cmax, findings, exposure multiples, NOAEL, etc.)
   - Enables fast semantic + keyword + structural search.
5. **CTD Generation Layer**
   - Module 2.4 (Overview) composer
   - Module 2.6 (Written Summary) composer
   - Uses:
     - Structured DB data
     - LLM synthesis
     - Configurable templates per tenant/user
6. **User Configuration System**
   - Prompt overrides
   - Template overrides
   - Temperature / style settings
   - Mapping rules
   - Stored at tenant or user level

---

# **2. Workflow Step-by-Step**

---

## **Step 1 — Upload & Storage**

### **Input**

- One or more PDFs representing Module 4 studies.

### **Process**

- Validate file metadata.
- Compute SHA-256 hash for deduplication.
- Store file in S3/Supabase bucket:
  <pre class="overflow-visible!" data-start="2327" data-end="2397"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>/tenant/{tenant_id}/project/{project_id}/raw/{file_id}.pdf
  </span></span></code></div></div></pre>

### **DB Writes**

- `documents` row
- `document_versions` row
- Status = `"uploaded"`

---

## **Step 2 — PDF Processing Pipeline**

### **Components**

- **Text extraction** (PyMuPDF)
- **OCR** (Tesseract or AWS Textract)
- **Table extraction** (Camelot + LLM cleanup)
- **Image extraction** (for histo or graphs)
- **Section detection** :
- Regex for patterns:
  - `^\s*(\d\.)+\s+[A-Za-z]`
  - `3.1.4.2 Dose Formulation Analysis`
- LLM fallback segmentation.

### **Output**

- `full_text`
- `sections[]`
- `tables[]`
- `images[]`

### **DB Writes**

- `raw_text`
- `structured_sections`
- `structured_tables`
- `structured_images`

Status → `"processed"`

---

## **Step 3 — Automatic Document Classification**

### **LLM Classifier Prompt**

- Determine study type:
  - Repeat-dose GLP tox
  - Safety pharmacology
  - TK / PK
  - Dose formulation
  - Analytical bioanalysis
  - Antibody assay
  - Histopathology report
  - Study protocol
  - Amendment
- Detect species and route:
  - Monkey, Rat, Dog
  - IV, SC, PO, IM
- Detect duration:
  - 4-week, 13-week, acute, chronic

### **DB Writes**

- `doc_type`
- `species`
- `route`
- `study_duration`

Status → `"classified"`

---

# **4. Bulk Extraction of Study Parameters (LLM Extractors)**

### Extractors run in series:

| Extractor                    | Output                                      |
| ---------------------------- | ------------------------------------------- |
| **NOAEL Extractor**          | species-specific NOAEL per sex, per study   |
| **Dose Group Extractor**     | dose table: mg/kg, n/sex, regimen           |
| **TK Parameters Extractor**  | Cmax, AUC, t1/2, exposure multiples         |
| **PK Parameters Extractor**  | absorption, distribution, elimination       |
| **Findings Extractor**       | clinical obs, clinical chemistry, pathology |
| **Histopathology Extractor** | organs affected, severity, incidence        |
| **Safety Pharm Extractor**   | CV, CNS, respiratory endpoints              |
| **GLP Compliance Extractor** | QA statements, deviations                   |
| **Study Metadata Extractor** | labs, dates, signatures                     |

All extracted values are stored in highly structured child tables.

---

# **5. Build the Nonclinical Dictionary**

The dictionary is used for:

- Fast **feature-based search**
- Fast **topic lookup**
- Automatic **CTD section-data mapping**

### **Dictionary Contains**

| Key Type        | Example                                             |
| --------------- | --------------------------------------------------- |
| Section numbers | `"4.2.1.3"`                                         |
| Section titles  | `"Dose Formulation Analysis"`                       |
| Topics          | `"NOAEL"`,`"TK exposure"`,`"clinical chemistry"`    |
| Entities        | `"ALT"`,`"Cmax"`,`"monkey plasma"`,`"heart weight"` |
| Values          | `AUC = 34500 ng·hr/mL`                              |
| Study labels    | `"0436RL35-001"`                                    |

Every piece of text extracted is matched to:

- Entities
- Topics
- Sections
- Study metadata

Stored in:

- `dictionary_terms`
- `dictionary_sections`
- `dictionary_topics`
- `dictionary_entities`

---

# **6. CTD Mapping Engine**

### **Inputs**

- Dictionary matches
- Study classification
- Extracted parameters
- Templates defined per CTD section

### **Example Mappings**

| CTD 2.6 Section             | Data Source                                 |
| --------------------------- | ------------------------------------------- |
| 2.6.1.1 Repeat-Dose Summary | NOAEL extractor + histopathology extractor  |
| 2.6.4.2 Toxicokinetics      | TK extractor                                |
| 2.6.2.2 Safety Pharm        | Safety pharm extractor                      |
| 2.4 Overview                | All high-level metadata + dictionary topics |

---

# **7. Generate CTD Sections with LLM**

### For each CTD section:

1. Retrieve required dictionary keys
2. Load study metadata
3. Load extracted entities
4. Compose a structured prompt
5. Apply user/tenant configuration (temperature, style, templates)
6. Produce:
   - Full text
   - Embedded references to source text
   - JSON summary

### Example Output:

<pre class="overflow-visible!" data-start="6051" data-end="6226"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Section</span><span></span><span>2.6</span><span>.1</span><span></span><span>Repeat-Dose</span><span></span><span>Toxicity</span><span></span><span>Summary</span><span>

</span><span>•</span><span></span><span>Species:</span><span></span><span>Cynomolgus</span><span></span><span>monkey</span><span>  
</span><span>•</span><span></span><span>Duration:</span><span></span><span>4</span><span></span><span>weeks</span><span></span><span>IV</span><span>  
</span><span>•</span><span></span><span>NOAEL:</span><span></span><span>50</span><span></span><span>mg/kg</span><span>  
</span><span>•</span><span></span><span>Key findings:</span><span></span><span>...</span><span>  
</span><span>•</span><span></span><span>Exposure multiples:</span><span></span><span>...</span><span>
</span></span></code></div></div></pre>

Stored in:

- `ctd_section_outputs`

---

# **8. Final Assembly**

A final CTD 2.4 or 2.6 document is assembled:

- Merged section texts
- Table of contents
- JSON metadata block
- PDF or DOCX export

Saved to:

`/tenant/{tenant}/project/{project}/ctd/ctd-2.6-{timestamp}.docx`

---

# **9. Status Tracking**

Pipeline status progression:

1. `uploaded`
2. `processed`
3. `classified`
4. `extracted`
5. `dictionary_built`
6. `ctd_generated`
7. `completed`

---

# **10. Error Handling**

- Missing NOAEL
- Missing TK tables
- OCR failure
- Ambiguous classification

Errors stored in:

- `ingestion_errors`

A retry queue allows reprocessing.

---

# **11. Database Pipeline Map (Supabase / NCD)**

- Upload → `documents` (logical) → `document_versions` (physical with hash/bucket/key/version).
- Extraction → `extraction_runs` (module/extractor/model/status).
- Structured outputs → `ncd_studies` (study metadata) plus `ncd_noael` and `ncd_pk_parameters`.
- Traceability → `extracted_entities` ties each NOAEL/PK row back to `extraction_runs` and `document_versions` with anchors for highlighting.
- Reviewer QA (optional) → `ncd_validation` status/comments per entity.

### Local Smoke Test (populates every table)

1. Make sure the schema is applied to your target DB (psql URL in `alembic.ini` or `$DATABASE_URL`).
2. Run:
   ```shell
   psql "$DATABASE_URL" -f scripts/ncd_schema_smoke_test.sql
   ```
3. Verify rows landed:
   ```shell
   psql "$DATABASE_URL" -c "TABLE documents;"          # Demo Study Document
   psql "$DATABASE_URL" -c "TABLE extracted_entities;" # NOAEL + PK_PARAM links
   psql "$DATABASE_URL" -c "TABLE ncd_validation;"
   ```

The smoke script seeds a full demo flow end-to-end: document + version, extraction_run, study, NOAEL/PK rows, `extracted_entities` links (with anchors), and an accepted `ncd_validation`. It is idempotent for the demo identifiers so you can rerun safely.

---

# **12. Full Workflow Diagram**

<pre class="overflow-visible!" data-start="6922" data-end="7258"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>PDF Upload
    ↓
PDF Processor → </span><span>text</span><span>, OCR, </span><span>tables</span><span>, sections
    ↓
Auto Classifier → tox/PK/safety pharm/etc.
    ↓
LLM Extractors → NOAEL, TK, findings
    ↓
</span><span>Dictionary</span><span> Builder → topics/sections/entities
    ↓
CTD Mapper → </span><span>mapping</span><span> keys </span><span>to</span><span> CTD templates
    ↓
CTD Generator → </span><span>2.4</span><span> / </span><span>2.6</span><span> auto </span><span>text</span><span>
    ↓
Export → DOCX / </span><span>JSON</span><span> / PDF</span></span></code></div></div></pre>
