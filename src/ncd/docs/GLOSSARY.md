# Glossary

IND AI Platform — Terminology & Definitions

This glossary provides standardized definitions for toxicology, pharmacokinetics, pathology, regulatory, and AI terminology used throughout the IND PDF Analysis system.

It is designed to support:

- consistent LLM extraction
- dictionary normalization
- CTD summary generation
- user comprehension across multidisciplinary teams

---

# **1. Nonclinical & Toxicology Terms**

### **NOAEL**

_No Observed Adverse Effect Level_ —

Highest dose at which no adverse effect is observed in a toxicity study.

### **LOAEL**

_Lowest Observed Adverse Effect Level_ —

Lowest dose at which an adverse effect is observed.

### **MTD**

_Maximum Tolerated Dose_ —

Highest dose producing acceptable toxicity.

### **HDT**

_Highest Dose Tested_ in a study.

### **TK**

_Toxicokinetics_ —

Assessment of systemic exposure during toxicity studies, usually Cmax, AUC, CL, Vd, T1/2.

### **AE / TEAEs**

_Adverse Effects / Treatment-Emergent Adverse Effects_

### **Target Organ Toxicity**

Organ system where toxicity consistently appears (e.g., liver, kidney, spleen).

### **Reversibility**

Whether findings resolve after a recovery period.

### **GLP**

_Good Laboratory Practice_ — quality system under which nonclinical safety studies are conducted.

### **Non-GLP**

Screening or exploratory studies not conducted to GLP standards.

### **Systemic Exposure**

Drug concentration in blood/plasma; measured as Cmax, AUC, etc.

---

# **2. Pharmacokinetic (PK) Terms**

### **Cmax**

Maximum observed plasma concentration.

### **Tmax**

Time at which Cmax occurs.

### **AUC**

Area Under the plasma concentration-time Curve — measure of overall exposure.

### **T1/2 (Half-life)**

Time required for plasma concentration to decrease by 50%.

### **CL (Clearance)**

Volume of plasma cleared per unit time.

### **Vd (Volume of Distribution)**

Theoretical volume required to contain the drug at observed concentration.

### **Dose Proportionality**

Whether PK increases linearly with dose.

### **Accumulation**

Increased exposure after repeat dosing.

---

# **3. Pathology & Organ System Terms**

### **Histopathology**

Microscopic evaluation of tissues for toxic effects.

### **Clinical Pathology**

Blood/urine evaluation:

- hematology
- clinical chemistry
- coagulation
- urinalysis

### **Hepatotoxicity**

Liver-related toxicity; often ALT/AST increases.

### **Nephrotoxicity**

Kidney toxicity; creatinine/BUN increases.

### **Cardiotoxicity**

Heart-related toxicity; ECG/QTc changes.

### **Myelosuppression**

Suppression of bone marrow cell production.

### **Immunotoxicity**

Effects on immune cells or immune function.

---

# **4. Study Design Terms**

### **Species**

Common laboratory animals:

- Rat
- Mouse
- Dog
- Non-human primate (cynomolgus, rhesus)

### **Route of Administration**

- IV (intravenous)
- SC (subcutaneous)
- PO (oral)
- IM (intramuscular)
- IP (intraperitoneal)

### **Satellite Groups**

Groups included for TK, recovery, or special assessments.

### **Recovery Animals**

Animals monitored after dosing stops to assess reversibility.

### **Dose Groups**

Groups assigned different dose levels, e.g.:

- Low
- Mid
- High
- Control

---

# **5. IND / CTD Regulatory Terms**

### **IND**

Investigational New Drug application submitted to FDA.

### **CTD**

_Common Technical Document_ — regulatory submission format accepted worldwide.

### **Module 2.4**

Nonclinical overview: a high-level narrative summarizing nonclinical data.

### **Module 2.6**

Nonclinical written summaries:

- 2.6.1 PK
- 2.6.2 Pharmacology
- 2.6.3 Toxicology
- 2.6.6 Integrated summary of toxicity

### **Module 4**

Full nonclinical study reports.

### **eCTD**

Electronic CTD — digital format for regulatory submissions.

---

# **6. AI / Machine Learning Terms**

### **LLM**

Large Language Model (GPT, Llama, etc.), used for:

- classification
- extraction
- summarization

### **Embeddings**

Numerical vector representation of text for semantic similarity search.

### **RAG**

Retrieval-Augmented Generation — LLM uses retrieved context for grounded output.

### **Prompt Template**

System-defined instruction used to guide LLM behavior.

### **Temperature**

Controls creativity in LLM responses.

### **Confidence Score**

Model-estimated confidence for extraction accuracy.

---

# **7. Platform-Specific Terms**

### **Dynamic Dictionary**

A knowledge layer storing:

- detected topics
- synonyms
- normalized terminology
- finding types
- organ systems
- PK terms
- mapping rules

  Used for search, extraction, and CTD summarization.

### **Chunking Engine**

Splits text into manageable sections for embedding and LLM analysis.

### **CTD Mapping Engine**

Automatically assigns extracted study content to CTD sections.

### **Ingestion Pipeline**

Steps:

- PDF ingestion
- extraction
- classification
- dictionary update
- vector indexing
- summary generation
