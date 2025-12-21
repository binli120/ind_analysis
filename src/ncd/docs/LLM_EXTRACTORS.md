# LLM Extractors


LLM-Based Extraction Framework for IND Nonclinical Documents

IND PDF Analysis Platform

This document describes the design, components, logic, and configuration of the **LLM Extractor Layer** used for converting unstructured Module 4 study PDFs into structured, machine-readable nonclinical data.

Extractors support all major study types:

* Toxicology
* Pharmacokinetics (PK)
* Toxicokinetics (TK)
* Safety Pharmacology
* Reproductive Toxicology
* Genotoxicity
* Carcinogenicity

Each extractor is modular, versioned, auditable, and backed by both pattern-based checks and LLM reasoning.

---

# **1. Overview of the Extractor Architecture**

<pre class="overflow-visible!" data-start="860" data-end="2042"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>               +</span><span>-----------------------------+</span><span>
               |         LLM Extractor       |
               +</span><span>---------------+-------------+</span><span>
                               |
                               v
              +</span><span>-------------------------------+</span><span>
              |  Chunk Retrieval (Vector DB)   |
              +</span><span>-------------------------------+</span><span>
                               |
                               v
                 +</span><span>-------------------------+</span><span>
                 | Prompt </span><span>Template</span><span> Engine  |
                 +</span><span>------------+------------+</span><span>
                              |
                              v
               +</span><span>-----------------------------+</span><span>
               |  LLM </span><span>JSON</span><span> Extraction Pass   |
               +</span><span>-----------------------------+</span><span>
                              |
                              v
             +</span><span>---------------------------------+</span><span>
             |  Validation & Normalization     |
             +</span><span>---------------------------------+</span><span>
                              |
                              v
               +</span><span>-----------------------------+</span><span>
               |   </span><span>Write</span><span></span><span>to</span><span></span><span>Database</span><span></span><span>Tables</span><span>   |
               +</span><span>-----------------------------+</span><span>
</span></span></code></div></div></pre>

---

# **2. Extraction Categories**

LLM extractors operate in the following domains:

## **2.1 Toxicology Extractors**

Used for repeat-dose, single-dose, safety pharm, and specialized tox studies.

Extract:

* NOAEL / LOAEL
* Target organ toxicity
* Major findings by organ system
* Severity (minimal, mild, moderate, marked)
* Reversibility
* Dose levels (mg/kg)
* Study design metadata
* Mortality findings
* Clinical pathology changes
* Recovery group results

## **2.2 PK / TK Extractors**

Used for single-dose or repeat-dose PK/TK studies.

Extract:

* Cmax
* AUC
* Tmax
* T1/2
* CL, Vd
* Exposure multiples
* Dose proportionality
* Accumulation ratio
* TK → NOAEL relationship (if applicable)

## **2.3 Table Extractors**

(studied text + table recognition)

Extract tables into structured objects:

* TK concentration-time tables
* PK summary tables
* Dose group sheets
* Hematology/biochemistry

Converted into arrays:

<pre class="overflow-visible!" data-start="3015" data-end="3164"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"table_name"</span><span>:</span><span></span><span>"PK Parameters"</span><span>,</span><span>
  </span><span>"columns"</span><span>:</span><span></span><span>[</span><span>"Group"</span><span>,</span><span></span><span>"Cmax"</span><span>,</span><span></span><span>"AUC"</span><span>,</span><span></span><span>"T1/2"</span><span>]</span><span>,</span><span>
  </span><span>"rows"</span><span>:</span><span></span><span>[</span><span>
    </span><span>[</span><span>"Low Dose"</span><span>,</span><span></span><span>"123"</span><span>,</span><span></span><span>"456"</span><span>,</span><span></span><span>"3.2"</span><span>]</span><span>
  </span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# **3. Extractor Lifecycle**

Each extractor follows a 6-step lifecycle:

<pre class="overflow-visible!" data-start="3245" data-end="3332"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>retrieve_chunks → build_prompt → LLM_call → parse </span><span>JSON</span><span> → </span><span>validate</span><span> → write_to_db
</span></span></code></div></div></pre>

## **3.1 Chunk Retrieval**

Chunks are selected based on:

* topic tags
* section headings
* semantic similarity
* dictionary topics
* search queries
* predefined rules (e.g., TK tables near Section 4.2.1.1)

## **3.2 Prompt Template Construction**

Combines:

* system prompt
* instruction prompt
* extraction schema
* example inputs/outputs
* user/tenant prompt overrides

Example skeleton:

<pre class="overflow-visible!" data-start="3748" data-end="3872"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>You are an expert </span><span>in</span><span> nonclinical toxicology.
Extract </span><span>only</span><span> the requested structured </span><span>JSON</span><span> fields.
</span><span>Return</span><span></span><span>nothing</span><span></span><span>else</span><span>.
</span></span></code></div></div></pre>

## **3.3 LLM JSON Mode Extraction**

LLM receives:

* study chunks
* extraction instructions
* JSON schema

Example:

<pre class="overflow-visible!" data-start="3997" data-end="4108"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"study_design"</span><span>:</span><span></span><span>{</span><span>...</span><span>}</span><span>,</span><span>
  </span><span>"dose_groups"</span><span>:</span><span></span><span>[</span><span>...</span><span>]</span><span>,</span><span>
  </span><span>"noael"</span><span>:</span><span></span><span>"50 mg/kg/day"</span><span>,</span><span>
  </span><span>"findings"</span><span>:</span><span></span><span>[</span><span>...</span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

## **3.4 Parsing and Error Handling**

Robust parsing includes:

* JSON validation
* fallback re-attempts
* hallucination suppression
* unit normalization (mg/kg, ng/mL)
* date normalization
* summary completeness checks

## **3.5 Normalization**

Convert free text into structured entities:

* organ → controlled vocabulary
* severity → minimal/mild/moderate/marked
* units → unified
* findings → canonical phrasing

## **3.6 Write to Database**

Saved into:

* `ncd_extraction_results`
* `ncd_study`
* `ncd_dose_group`
* `ncd_exposure_metric`
* `ncd_finding`
* `ncd_study_safety_summary`

---

# **4. Toxicology Extractor: Detailed Fields**

### **4.1 Study Metadata**

* species
* strain
* route
* duration
* GLP status
* number of animals
* satellite groups

### **4.2 Dose Groups**

Includes:

<pre class="overflow-visible!" data-start="4953" data-end="5044"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"dose_mg_per_kg"</span><span>:</span><span></span><span>50</span><span>,</span><span>
  </span><span>"n"</span><span>:</span><span></span><span>10</span><span>,</span><span>
  </span><span>"sex"</span><span>:</span><span></span><span>"M/F"</span><span>,</span><span>
  </span><span>"recovery_group"</span><span>:</span><span></span><span>true</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

### **4.3 NOAEL / LOAEL**

Logic enforced:

* consistent across organ systems
* must match exposure values if available

### **4.4 Findings Extraction**

List of findings by organ system:

<pre class="overflow-visible!" data-start="5236" data-end="5359"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>liver:
  - centrilobular </span><span>hypertrophy</span><span></span><span>(mild, reversible)</span><span>
kidney:
  - tubular </span><span>degeneration</span><span></span><span>(moderate, non-reversible)</span><span>
</span></span></code></div></div></pre>

### **4.5 Reversibility Assessment**

Extracted from recovery groups or necropsy results.

---

# **5. PK / TK Extractor: Detailed Fields**

### **5.1 PK Values**

* Cmax
* AUC(0-t)
* AUC(0-inf)
* Tmax
* T1/2
* CL
* Vd

### **5.2 Exposure Multiples**

Calculated vs human predicted exposure if available.

### **5.3 TK Table Extraction**

TK tables often embedded in PDF; the extractor:

* identifies TK sections
* extracts table headers
* extracts per-animal or per-group values
* normalizes to numeric values

---

# **6. Error Handling & Reconciliation**

Extractors apply:

* confidence scoring
* cross-validation with table data
* cross-validation with dose group definitions
* CTD consistency checks

On conflicts:

* the system selects values with highest evidence alignment
* users may manually override results

---

# **7. Configuration Integration**

Each extractor can be overridden at:

* system level
* tenant (company) level
* user level

Examples:

### Override extraction temperature:

<pre class="overflow-visible!" data-start="6398" data-end="6434"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"temperature"</span><span>:</span><span></span><span>0.1</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

### Override toxicology extraction prompt:

<pre class="overflow-visible!" data-start="6479" data-end="6560"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"extract_findings"</span><span>:</span><span></span><span>"Use concise bullet points for findings..."</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

### Override PK table extraction rules:

<pre class="overflow-visible!" data-start="6602" data-end="6694"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"extract_pk_table"</span><span>:</span><span></span><span>{</span><span>
    </span><span>"required_columns"</span><span>:</span><span></span><span>[</span><span>"Group"</span><span>,</span><span></span><span>"Cmax"</span><span>,</span><span></span><span>"AUC"</span><span>]</span><span>
  </span><span>}</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# **8. Extractor Versioning**

Each extraction output includes:

* extractor name
* version
* model used
* timestamp
* provenance data
* source chunk IDs

Example:

<pre class="overflow-visible!" data-start="6878" data-end="6993"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"extractor"</span><span>:</span><span></span><span>"tox_v2"</span><span>,</span><span>
  </span><span>"model"</span><span>:</span><span></span><span>"gpt-4.1"</span><span>,</span><span>
  </span><span>"version"</span><span>:</span><span></span><span>"2.0.1"</span><span>,</span><span>
  </span><span>"chunks"</span><span>:</span><span></span><span>[</span><span>"uuid1"</span><span>,</span><span></span><span>"uuid2"</span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

This provides regulatory traceability.

---

# **9. Future Enhancements**

* multi-model ensembles
* numeric evaluation cross-checks against human PK simulations
* specialized extractors (immunogenicity, neurotox)
* auto-prompt optimization
* dataset-based finetuning
