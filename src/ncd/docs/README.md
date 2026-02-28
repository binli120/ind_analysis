@copyright longooc.com
@author: Bin Lee
@email: blee@longooc.com

# IND PDF Analysis

AI-Powered Nonclinical Module Generator for CTD 2.4 & 2.6

IND PDF Analysis is a modular system designed to automatically convert large sets of IND Module 4 nonclinical study PDFs into structured data that can be used to generate CTD Module 2.4 and 2.6 summaries.

The system performs:

- PDF ingestion
- Text extraction
- Intelligent chunking
- Section and topic detection
- LLM-based study classification (tox/PK/safety pharm/etc.)
- LLM-based extraction (NOAEL, findings, PK metrics)
- Automatic CTD section mapping
- Narrative generation for 2.4 and 2.6

Everything is configurable at user, tenant, and system levels.

---

# 🌟 Features

- **PDF → Page Extraction**
- **Text Chunking Engine**
- **Section Number Detection (e.g., 4.2.3.3)**
- **Topic Detection (hepatotoxicity, PK, QT, etc.)**
- **Semantic Embeddings for Fast Search**
- **Study Type Classification**
- **LLM-powered Extraction**
  - NOAEL / LOAEL
  - Target Organs
  - Key Findings
  - Dose Groups
  - PK parameters (Cmax, AUC, t₁/₂)
- **Automatic CTD Mapping**
- **CTD 2.4 + 2.6 Narrative Generation**

# 🧱 High-Level Architecture

+--------------------------+
| PDF Ingestion |
| (source docs + pages) |
+-------------+------------+
|
v
+--------------------------+
| Text Chunking |
| Sections + Topics + Meta |
+-------------+------------+
|
v
+--------------------------+
| Embedding Generator |
| (vector search ready) |
+-------------+------------+
|
v
+--------------------------+
| Study Classification |
| (repeat-dose, PK, etc.) |
+-------------+------------+
|
v
+--------------------------+
| LLM Data Extraction |
| (NOAEL, PK, findings) |
+-------------+------------+
|
v
+--------------------------+
| CTD Mapping Engine |
| (2.6.6.x assignments) |
+-------------+------------+
|
v
+--------------------------+
| Summary Generator |
| (CTD 2.4 / 2.6 text) |
+--------------------------+

---

# 📁 Repository Structure

---

ncd/

ingestion/

extraction/

classification/

mapping/

generation/

api/

schemas.py

db.py

config.py

llm_client.py

---

# ⚙️ Installation

### 1. Clone repository

```bash
git clone https://github.com/your-org/ind-pdf-analysis.git
cd ind-pdf-analysis
---
```
