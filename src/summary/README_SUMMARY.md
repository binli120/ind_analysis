# IND Section 2.4 Generation Platform

> **Automated generation of IND Section 2.4 (Nonclinical Overview) from Section 2.6 (Nonclinical Summaries) with intelligent gap analysis and AI-powered summarization**

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4-412991.svg)](https://openai.com/)

---

## 🎯 Overview

This platform automates the creation of regulatory submission documents for Investigational New Drug (IND) applications. It processes Section 2.6 content (detailed nonclinical summaries), performs comprehensive gap analysis, and generates a high-quality Section 2.4 Nonclinical Overview using advanced AI.

### Key Features

- ✅ **Automated Document Scanning** - Processes PDF, Word, and Excel files
- ✅ **Intelligent Gap Analysis** - Identifies missing required content with severity assessment
- ✅ **AI-Powered Summarization** - Uses GPT-4 to generate regulatory-compliant summaries
- ✅ **Completeness Scoring** - Quantifies submission readiness (0-100%)
- ✅ **Placeholder Management** - Inserts actionable placeholders for missing content
- ✅ **Structured Output** - Generates JSON with detailed metadata and analysis

---

## 📋 Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Documentation](#documentation)
- [Features](#features)
- [Architecture](#architecture)
- [Output Examples](#output-examples)
- [Regulatory Compliance](#regulatory-compliance)
- [Advanced Usage](#advanced-usage)
- [Contributing](#contributing)
- [License](#license)

---

## 🚀 Installation

### Prerequisites

- Python 3.8 or higher
- OpenAI API key ([Get one here](https://platform.openai.com/api-keys))
- Section 2.6 documents in PDF or Word format

### New: S3-driven 2.4 generation (LT1009 example)

Use the new `IND24GenerationPipeline` when your Section 2.6 source lives in S3 and markdown sidecars are stored in Redis (or S3). The helper will:

- Pull Section 2.6 markdown for the project (fallback: generate from PDFs when `.md` is missing)
- Concatenate content for OpenAI
- Run gap analysis using `IND_2.4_Generation_Guideline.md`
- Generate a Section 2.4 JSON summary via `ind_2_4_generation_template.json`
- Save combined markdown, gap report, and summary JSON back into the Section 2.6 folder in S3

```python
from summary import IND24GenerationConfig, IND24GenerationPipeline

config = IND24GenerationConfig(
    bucket="doc-repository-dev",
    project="LT1009",
    section_prefix="filynai.com/LT1009/Module 2. CTD summaries/us/2.6 Nonclinical Summary",
    redis_url="redis://localhost:6379/0",  # optional cache
)

pipeline = IND24GenerationPipeline(config)
result = pipeline.run()
print(result["output_keys"])
```

Ensure `OPENAI_API_KEY` is set and that the container/host has AWS credentials allowing read/write on the target bucket.

### CLI (auto-discovery of 2.6 prefix)

```
poetry run python scripts/generate_ind24.py \
  --bucket <bucket> \
  --company filynai.com \
  --project LT1009 \
  --redis-url redis://localhost:6379/0 \
  --log-level INFO
```

Outputs (written alongside the detected 2.6 prefix):
- `section_2_6_combined.md`
- `section_2_6_gap_analysis.md`
- `section_2_6_gap_analysis.json`
- `section_2_4_summary.md`

Flags:
- `--section-prefix` to override auto-discovery
- `--max-chunks`, `--log-level DEBUG`, `--auto-discover-prefix` (on by default)

### Basic Installation

```bash
# Clone the repository
git clone https://github.com/your-org/ind-24-generator.git
cd ind-24-generator

# Install dependencies
pip install -r requirements.txt
```

### Using Virtual Environment (Recommended)

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## ⚡ Quick Start

### 1. Configure Your Environment

```bash
# Copy configuration template
cp config.template.env .env

# Edit configuration
nano .env  # or use your preferred editor
```

Minimum required settings:
```env
OPENAI_API_KEY=sk-your-api-key-here
SECTION_26_FOLDER=/path/to/your/section_2.6
```

### 2. Organize Your Documents

```
section_2.6/
├── 2.6.2_Pharmacology.pdf
├── 2.6.3_Pharmacokinetics.pdf
├── 2.6.4_Toxicology.pdf
└── 2.6.6_Toxicokinetics.pdf
```

### 3. Run the Generator

```bash
python quick_start_example.py
```

Or use Python API:

```python
from ind_2_4_generator import IND24Pipeline

pipeline = IND24Pipeline(
    section_26_folder="/path/to/section_2.6",
    openai_api_key="your-api-key"
)

results = pipeline.run()
print(f"Completeness: {results['section_2_4']['document_metadata']['completeness_score']}%")
```

### 4. Review Output

The system generates:
- `ind_2_4_output_YYYYMMDD_HHMMSS.json` - Complete structured results
- Console output with gap analysis
- Completeness score and regulatory readiness assessment

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [Quick Start Guide](QUICK_START.md) | Get up and running in 5 minutes |
| [IND 2.4 Generation Guideline](IND_2.4_Generation_Guideline.md) | Comprehensive operational guide |
| [Advanced Features](ADVANCED_FEATURES.md) | System architecture and customization |
| [API Reference](#) | Detailed API documentation |

---

## ✨ Features

### Document Processing

- **Multi-format Support**: PDF, Word (.docx), Excel (.xlsx)
- **Intelligent Section Detection**: Automatically identifies Section 2.6 subsections
- **Content Extraction**: Preserves formatting and structure
- **Metadata Collection**: Tracks sources, page numbers, and word counts

### Gap Analysis

- **Comprehensive Validation**: Checks for all FDA-required content
- **Severity Assessment**: Classifies gaps as CRITICAL, MAJOR, or MINOR
- **Regulatory Impact**: Explains consequences of missing content
- **Actionable Recommendations**: Provides specific next steps

### AI-Powered Generation

- **GPT-4 Integration**: Uses latest OpenAI models for high-quality output
- **Structured Prompting**: Engineered prompts for regulatory compliance
- **Section-by-Section**: Generates 2.4.1 through 2.4.5 systematically
- **Quality Control**: Validates output for accuracy and completeness

### Quality Assurance

- **Completeness Scoring**: Quantitative assessment (0-100%)
- **Readiness Assessment**: READY / NEEDS_MINOR_UPDATES / NEEDS_MAJOR_UPDATES / NOT_READY
- **Source Validation**: Cross-references generated content with source
- **Human Review Flags**: Highlights areas requiring manual verification

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│               IND24Pipeline                         │
│          (Main Orchestration Layer)                 │
└─────────────────────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Document    │ │     Gap      │ │   OpenAI     │
│   Scanner    │ │   Analyzer   │ │  Generator   │
└──────────────┘ └──────────────┘ └──────────────┘
        │               │               │
        ▼               ▼               ▼
┌─────────────────────────────────────────────────────┐
│              Structured JSON Output                  │
│   - Metadata    - Content    - Gap Analysis         │
└─────────────────────────────────────────────────────┘
```

### Core Components

**DocumentScanner**
- Scans folders for 2.6 documents
- Extracts text from multiple formats
- Identifies section numbers
- Detects subsections using keywords

**GapAnalyzer**
- Validates content against requirements
- Classifies missing/incomplete sections
- Assigns severity levels
- Generates actionable reports

**Section24Generator**
- Constructs structured prompts
- Calls OpenAI API with function calling
- Parses and validates responses
- Inserts placeholders for gaps

---

## 📊 Output Examples

### Console Output

```
================================================================================
IND SECTION 2.4 GENERATION PIPELINE
================================================================================

[1/5] Scanning Section 2.6 folder...
   Found 12 files (8 PDFs, 3 Word docs)

[2/5] Extracting content from documents...
   Extracted 4 sections
   - 2.6.2: 3,542 words
   - 2.6.3: 2,890 words
   - 2.6.4: 8,234 words
   - 2.6.6: 1,456 words

[3/5] Performing gap analysis...
   Found 1 gaps

GAP ANALYSIS REPORT
================================================================================
MAJOR GAPS (Should be addressed):
--------------------------------------------------------------------------------
• 2.6.4 - Toxicology Written Summary
  Issue: Missing subsections: reproductive_fertility
  Action: Provide data for: reproductive_fertility

[4/5] Generating Section 2.4 summary with OpenAI...
   Completeness: 85%
   Regulatory Readiness: NEEDS_MINOR_UPDATES

[5/5] Saving results...
   Results saved to: ind_2_4_output_20241125_143022.json

================================================================================
PIPELINE COMPLETE
================================================================================
```

### Gap Analysis Report

```json
{
  "gap_analysis": {
    "missing_sections": [],
    "incomplete_sections": [
      {
        "section_number": "2.6.4",
        "missing_elements": ["reproductive_fertility"],
        "severity": "MAJOR",
        "regulatory_impact": "May result in information request from FDA",
        "recommendation": "Complete fertility study before submission"
      }
    ]
  }
}
```

### Generated Summary Example

```json
{
  "section_2_4_content": {
    "2.4.4_toxicology_summary": {
      "repeat_dose_toxicity": {
        "text": "Repeat-dose toxicity studies were conducted in rats (28-day) and dogs (28-day) to characterize the toxicity profile...",
        "noael_summary": [
          {
            "species": "Rat",
            "duration": "28 days",
            "noael": "10 mg/kg/day",
            "target_organs": ["Liver", "Kidney"],
            "exposure_multiple": "50-fold"
          }
        ],
        "has_gaps": false
      }
    }
  }
}
```

---

## 📜 Regulatory Compliance

### Standards Followed

- **ICH M4**: Common Technical Document format
- **FDA Guidance**: IND Content and Format requirements
- **21 CFR 312**: IND regulations
- **ICH S6(R1)**: Nonclinical safety studies

### Section 2.4 Requirements

The platform ensures inclusion of:

✅ **2.4.1 Introduction**: Drug substance, nonclinical program overview  
✅ **2.4.2 Pharmacology**: Primary and safety pharmacology summary  
✅ **2.4.3 Pharmacokinetics**: ADME overview and exposure data  
✅ **2.4.4 Toxicology**: Repeat-dose, genotoxicity, reproductive toxicity  
✅ **2.4.5 Integrated Assessment**: Risk evaluation and clinical recommendations  

### Critical Content Checklist

| Content | Phase 1 Required | Detected By |
|---------|------------------|-------------|
| Repeat-dose toxicity (2 species) | ✅ CRITICAL | Keyword + NER |
| Genotoxicity battery (Ames, Chromosomal, Micronucleus) | ✅ CRITICAL | Keyword matching |
| Safety pharmacology (CV, CNS, Resp) | ✅ CRITICAL | Keyword matching |
| ADME profile | ✅ MAJOR | Keyword matching |
| Embryo-fetal development (WOCBP) | ⚠️ Conditional | Keyword matching |

---

## 🔧 Advanced Usage

### Custom Keywords

```python
# Customize detection keywords for your compound
CUSTOM_KEYWORDS = {
    "repeat_dose_toxicity": [
        "NOAEL", "repeat dose", 
        "your_compound_specific_term"
    ]
}
```

### Multi-Model Comparison

```python
# Compare outputs from multiple models
from ind_2_4_generator import MultiModelGenerator

generator = MultiModelGenerator(openai_key, anthropic_key)
results = generator.generate_ensemble(
    prompt, 
    models=['gpt-4-turbo', 'claude-3-opus']
)
best = generator.select_best_sections(results)
```

### REST API

```bash
# Start the API server
uvicorn api:app --reload --port 8000

# Submit documents
curl -X POST "http://localhost:8000/api/v1/generate" \
  -F "files=@2.6.4_Toxicology.pdf" \
  -F "drug_name=Drug X"

# Check status
curl "http://localhost:8000/api/v1/status/{job_id}"
```

For more advanced features, see [ADVANCED_FEATURES.md](ADVANCED_FEATURES.md).

---

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test
pytest tests/test_gap_analysis.py -v

# Run with coverage
pytest --cov=ind_2_4_generator tests/
```

---

## 📈 Performance

| Metric | Typical Value |
|--------|---------------|
| Document Scanning | 2-5 seconds per file |
| Gap Analysis | < 1 second |
| AI Generation | 30-60 seconds |
| **Total Pipeline** | **2-5 minutes** |

*Performance depends on document size and OpenAI API response time*

---

## 🛠️ Troubleshooting

### Common Issues

**"No sections detected"**
```bash
# Ensure files include section numbers in filename
mv Toxicology.pdf 2.6.4_Toxicology.pdf
```

**"OpenAI API timeout"**
```python
# Increase timeout in configuration
OPENAI_TIMEOUT=300  # 5 minutes
```

**"Low completeness score"**
```
# Check gap analysis report
# Verify content actually contains required information
# Update keywords if needed
```

For more troubleshooting, see [QUICK_START.md](QUICK_START.md).

---

## 📊 Roadmap

### Version 1.0 (Current)
- ✅ Basic document scanning
- ✅ Gap analysis
- ✅ OpenAI integration
- ✅ JSON output

### Version 1.1 (Q1 2025)
- [ ] Web UI
- [ ] Multi-language support
- [ ] Enhanced NOAEL extraction
- [ ] Word document export

### Version 2.0 (Q2 2025)
- [ ] Machine learning-based quality scoring
- [ ] Semantic search integration
- [ ] Multi-model ensemble
- [ ] Database persistence

### Future Enhancements
- [ ] Real-time collaboration
- [ ] Version control integration
- [ ] Automated submission to eCTD
- [ ] AI-powered study design recommendations

---

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### How to Contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- OpenAI for GPT-4 API
- FDA for comprehensive IND guidance
- ICH for M4 guidelines
- All contributors and testers

---

## 📞 Support

- 📧 Email: support@example.com
- 💬 Slack: [Join our community](https://slack.example.com)
- 📖 Documentation: [docs.example.com](https://docs.example.com)
- 🐛 Issues: [GitHub Issues](https://github.com/your-org/ind-24-generator/issues)

---

## 📈 Statistics

![GitHub stars](https://img.shields.io/github/stars/your-org/ind-24-generator)
![GitHub forks](https://img.shields.io/github/forks/your-org/ind-24-generator)
![GitHub issues](https://img.shields.io/github/issues/your-org/ind-24-generator)
![GitHub pull requests](https://img.shields.io/github/issues-pr/your-org/ind-24-generator)

---

## 🎓 Citation

If you use this software in your regulatory submissions, please cite:

```bibtex
@software{ind24generator,
  title={IND Section 2.4 Generation Platform},
  author={Your Organization},
  year={2024},
  url={https://github.com/your-org/ind-24-generator}
}
```

---

**Built with ❤️ for the regulatory community**

*Making IND submissions faster, more accurate, and less painful.*
