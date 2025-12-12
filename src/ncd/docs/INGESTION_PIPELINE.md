# Ingestion Pipeline

# **📦 IND Ingestion Pipeline — End-to-End Architecture**

This document describes the full ingestion pipeline that converts raw IND Module 4 PDFs into a structured nonclinical knowledge graph, dynamic dictionary entries, searchable metadata, and finally CTD-ready summaries (Modules 2.4 and 2.6).

---

# **1. Pipeline Overview**

<pre class="overflow-visible!" data-start="504" data-end="739"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>[</span><span>PDF Upload</span><span>]
     ↓
[</span><span>Pre-Processing</span><span>]
     ↓
[</span><span>LLM Extraction Layer</span><span>]
     ↓
[</span><span>Dynamic Dictionary Builder</span><span>]
     ↓
[</span><span>Classification + Mapping</span><span>]
     ↓
[</span><span>Database Persistence</span><span>]
     ↓
[</span><span>Search Index Build</span><span>]
     ↓
[</span><span>CTD 2.4 / 2.6 Generator</span><span>]
</span></span></code></div></div></pre>

---

# **2. Step-by-Step Workflow**

## **2.1 File Intake**

- Accepts: **PDF, DOCX, TXT**
- Extracts:
  - study metadata (study number, species, duration)
  - document type (tox, PK, TK, safety pharm, BA/BE)
  - page count, hashes, tenant-owner mapping
- Stores in `documents_raw` table.

---

# **2.2 Text Extraction**

### Goals:

- Convert PDF → clean text
- Extract tables & images (stored separately)
- Preserve reading order

### Tools:

- **pdfminer / pymupdf**
- **table extractors** (Camelot, PDFPlumber)
- **OCR fallback** for scanned pages (Tesseract / OCRmyPDF)

### Output:

- `text_chunks`
- `table_json`
- `image_assets`
- `page_mapping`

Stored in `documents_processed`.

---

# **2.3 Section & Header Detection (LLM + Regex Hybrid)**

Each document is segmented into logical sections:

- CTD-like numbering (e.g., **4.2.1.3** )
- GLP study headings
- Common patterns ("Materials and Methods", "Results", "Toxicokinetics")

### Stored in:

`document_sections`

<pre class="overflow-visible!" data-start="1711" data-end="1825"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{</span><span>
  </span><span>"section_number"</span><span>:</span><span></span><span>"VI.A.3"</span><span>,</span><span>
  </span><span>"title"</span><span>:</span><span></span><span>"Dose Formulation Analysis"</span><span>,</span><span>
  </span><span>"start"</span><span>:</span><span></span><span>20430</span><span>,</span><span>
  </span><span>"end"</span><span>:</span><span></span><span>23991</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# **2.4 Study Type Classification (LLM)**

Each document gets auto-labeled:

- **Repeat-dose tox**
- **Safety pharmacology**
- **Single-dose PK**
- **TK**
- **Bioanalytical**
- **Histopathology-only**
- **Protocol / Amendment**
- **Deviation Report**

### Stored in:

`document_classification`

<pre class="overflow-visible!" data-start="2125" data-end="2262"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{</span><span>
  </span><span>"study_type"</span><span>:</span><span></span><span>"4-week repeat-dose GLP toxicity"</span><span>,</span><span>
  </span><span>"species"</span><span>:</span><span></span><span>"cynomolgus monkey"</span><span>,</span><span>
  </span><span>"route"</span><span>:</span><span></span><span>"IV"</span><span>,</span><span>
  </span><span>"duration"</span><span>:</span><span></span><span>"28 days"</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# **2.5 Parameter Extraction Layer (LLM)**

Each section is processed through purpose-built extractors:

## **Extracted Parameters**

### Toxicology

- NOAEL (species, sex)
- target organs of toxicity
- dose groups
- clinical signs
- body weight / food consumption findings
- clinical pathology changes (hema/chem/UA)
- microscopic pathology

### Toxicokinetics

- Cmax, AUC, t1/2, CL, Vz
- exposure margins
- accumulation ratio
- day-to-day changes

### Pharmacokinetics

- absorption
- distribution
- clearance
- dose proportionality

### Safety Pharmacology

- ECG, ophthalmology, neurobehavior, respiration

### Bioanalytical

- assay information
- calibration curve fit
- LLOQ/ULOQ

---

# **2.6 Dynamic Nonclinical Dictionary Builder**

Extracted entities feed the **tenant-level dictionary** .

Example dictionary nodes:

<pre class="overflow-visible!" data-start="3091" data-end="3278"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>NOAEL:</span><span>
  </span><span>-</span><span></span><span>rat:</span><span></span><span>50</span><span></span><span>mg/kg/day</span><span>
  </span><span>-</span><span></span><span>monkey:</span><span></span><span>10</span><span></span><span>mg/kg/day</span><span>

</span><span>Findings:</span><span>
  </span><span>-</span><span></span><span>ALT</span><span></span><span>↑</span><span></span><span>at</span><span></span><span>mid/high</span><span></span><span>dose</span><span>
  </span><span>-</span><span></span><span>decreased</span><span></span><span>food</span><span></span><span>consumption</span><span></span><span>at</span><span></span><span>150</span><span></span><span>mg/kg</span><span>

</span><span>Exposure:</span><span>
  </span><span>-</span><span></span><span>monkey Cmax Day 1:</span><span></span><span>450</span><span></span><span>µg/mL</span><span>
</span></span></code></div></div></pre>

### Stored in:

- `dictionary_topics`
- `dictionary_entities`
- `dictionary_key_values`

Supports:

- auto-suggestion in UI
- auto-fill for CTD 2.4/2.6
- global search

---

# **2.7 CTD Mapping Engine**

The pipeline maps extracted study data to CTD sections:

### CTD 2.6:

- PK Summary → 2.6.2
- Toxicology Summary → 2.6.6
- Local Tolerance → 2.6.7
- TK exposure → 2.6.4

### CTD 2.4:

- overall weight of evidence
- safety margin assessment
- risk/benefit summary

Results stored in:

`ctd_mapping_rules`

`ctd_structured_output`

---

# **2.8 Database Persistence**

All extracted objects are written under:

### **Core Tables**

- `documents_raw`
- `documents_processed`
- `document_sections`
- `document_classification`
- `document_parameters`
- `dictionary_topics`
- `dictionary_entities`
- `dictionary_key_values`
- `ctd_mapping_rules`
- `ctd_sections_generated`

All rows linked by:

- document_id
- section_id
- tenant_id
- project_id

---

# **2.9 Search Index Build**

Builds a full-text + vector search index:

### Enables:

- “show all NOAEL sections”
- “find TK exposure tables”
- “filter studies in monkey”
- “find all findings affecting kidney”

Stored in:

- `search_index`
- `vector_embeddings`

---

# **2.10 CTD Summary Generation**

Final step uses:

- extracted parameters
- dictionary values
- study metadata
- tenant templates
- custom prompts

### Generates:

- CTD **Module 2.4** (Nonclinical Overview)
- CTD **Module 2.6** (Nonclinical Summaries)
- Section-by-section granular outputs
- Reference mapping back to source text spans

Stored in:

`ctd_sections_generated`

---

# **3. Python Pipeline Structure**

Directory layout:

<pre class="overflow-visible!" data-start="4928" data-end="5225"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>/ingestion/
    pdf_loader.py
    preprocess.py
    section_detector.py
    llm_extractors/
        tox.py
        tk.py
        pk.py
        pathology.py
        assay.py
    dictionary_builder.py
    classifier.py
    ctd_mapper.py
    persist.py
    search_index.py
    run_pipeline.py
</span></span></code></div></div></pre>

---

# **4. Example Usage**

<pre class="overflow-visible!" data-start="5256" data-end="5423"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>from ingestion.run_pipeline </span><span>import</span><span> process_document

</span><span>process_document</span><span>(
    file_path=</span><span>"0436rl35.pdf"</span><span>,
    tenant_id=</span><span>"tenant_123"</span><span>,
    project_id=</span><span>"project_abc"</span><span>
)
</span></span></code></div></div></pre>

### Output:

<pre class="overflow-visible!" data-start="5437" data-end="5569"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{</span><span>
  </span><span>"status"</span><span>:</span><span></span><span>"complete"</span><span>,</span><span>
  </span><span>"document_id"</span><span>:</span><span></span><span>"doc_987"</span><span>,</span><span>
  </span><span>"sections"</span><span>:</span><span></span><span>42</span><span>,</span><span>
  </span><span>"parameters_extracted"</span><span>:</span><span></span><span>317</span><span>,</span><span>
  </span><span>"ctd_ready"</span><span>:</span><span></span><span>true</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# **5. Error Handling**

| Step              | Failure Mode        | Behavior                |
| ----------------- | ------------------- | ----------------------- |
| PDF extraction    | corrupted PDF       | fallback OCR            |
| LLM extraction    | missing values      | missing-data report     |
| Classification    | ambiguous           | return top-3 candidates |
| Dictionary update | conflicting entries | versioned update        |

---

# **6. Future Extensions**

- Multilingual PDFs
- Vendor-specific normalization modules (e.g., Charles River, Calvert)
- Image-to-pathology extraction (WSI integration)
- Human-in-the-loop QC interface
