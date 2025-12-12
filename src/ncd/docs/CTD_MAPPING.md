# CTD Mapping

# **📘 CTD MAPPING ENGINE — RULES & LOGIC**

This document explains how the ingestion pipeline maps extracted nonclinical data (toxicity, PK, TK, pathology, findings, NOAEL, exposure margins) into **ICH CTD Modules 2.4 and 2.6** .

The CTD mapping layer acts as the “brain” that determines:

1. **Which extracted information belongs in which CTD section**
2. **How multiple studies combine into summary conclusions**
3. **Which rules must be applied to produce regulatory-compliant text**
4. **How each generated CTD paragraph can link back to the source documents**

---

# **1. Overview of the Mapping Flow**

<pre class="overflow-visible!" data-start="804" data-end="1019"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>[</span><span>Extracted Data</span><span>] 
     ↓
[</span><span>Topic Normalization</span><span>]
     ↓
[</span><span>Mapping Rules Engine</span><span>]
     ↓
[</span><span>Section-Level Assignments</span><span>]
     ↓
[</span><span>Hierarchical CTD Structure Assembly</span><span>]
     ↓
[</span><span>Final 2.4 / 2.6 Summary Text + References</span><span>]
</span></span></code></div></div></pre>

CTD mapping is **NOT generation** — it is a deterministic assignment of structured data to the correct CTD section keys.

---

# **2. CTD Sections Supported**

## **Module 2.4 — Nonclinical Overview**

| Section   | Purpose                                      |
| --------- | -------------------------------------------- |
| **2.4.1** | Overview of the nonclinical testing strategy |
| **2.4.2** | Pharmacology overview                        |
| **2.4.3** | Pharmacokinetics overview                    |
| **2.4.4** | Toxicology overview                          |
| **2.4.5** | Integrated risk assessment                   |

---

## **Module 2.6 — Nonclinical Written & Tabulated Summaries**

### **2.6.1** — Introduction

### **2.6.2** — Pharmacology Summary

### **2.6.3** — Pharmacokinetics Summary

### **2.6.4** — Toxicokinetics Summary

### **2.6.5** — Local Tolerance (if applicable)

### **2.6.6** — Toxicity Summary

Each section is constructed from normalized extracted data.

---

# **3. Mapping Rules Model**

Each extracted datum is converted to:

<pre class="overflow-visible!" data-start="1930" data-end="2138"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{</span><span>
  </span><span>"topic"</span><span>:</span><span></span><span>"noael"</span><span>,</span><span>
  </span><span>"value"</span><span>:</span><span></span><span>"10 mg/kg"</span><span>,</span><span>
  </span><span>"species"</span><span>:</span><span></span><span>"monkey"</span><span>,</span><span>
  </span><span>"sex"</span><span>:</span><span></span><span>"male"</span><span>,</span><span>
  </span><span>"source_document_id"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
  </span><span>"source_section"</span><span>:</span><span></span><span>"Histopathology"</span><span>,</span><span>
  </span><span>"ctd_targets"</span><span>:</span><span></span><span>[</span><span>"2.6.6"</span><span>,</span><span></span><span>"2.4.4"</span><span>,</span><span></span><span>"2.4.5"</span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

The mapping rule engine determines the **ctd_targets** list.

---

# **4. Rule Types**

## **4.1 Direct Mapping Rules**

Some concepts always map to specific CTD locations.

| Extracted Topic                       | Mapped CTD Section(s) |
| ------------------------------------- | --------------------- |
| NOAEL                                 | 2.6.6, 2.4.4, 2.4.5   |
| Target organs of toxicity             | 2.6.6, 2.4.4          |
| Clinical pathology findings           | 2.6.6                 |
| Microscopic pathology                 | 2.6.6                 |
| TK parameters (Cmax, AUC, t1/2)       | 2.6.4                 |
| Exposure multiples                    | 2.6.4, 2.4.5          |
| PK ADME features                      | 2.6.3, 2.4.3          |
| Safety pharmacology ECG/ophthalmology | 2.6.2                 |
| Dose proportionality                  | 2.6.3                 |
| Linear vs nonlinear kinetics          | 2.6.3, 2.4.3          |

---

## **4.2 Contextual Mapping Rules**

Some assignments depend on metadata:

### **Species**

- Rodent/Nonrodent → toxicity summaries
- Monkey TK → exposure margin summary
- Rat single-dose PK → 2.6.3 only

### **Study Type**

- Repeat-dose tox → 2.6.6
- Safety pharmacology → 2.6.2
- Single-dose PK → 2.6.3
- TK → 2.6.4
- Bioanalytical validation → appendix (not CTD)
- Histopathology standalone → 2.6.6

### **Finding Severity**

- Severe findings → elevated prominence in 2.4.5 risk section
- Minimal findings → may be excluded unless dose-limiting

---

## **4.3 Derived Mapping Rules (Computed During Ingestion)**

### **Exposure Margin Calculation → 2.6.4 + 2.4.5**

Computed from:

<pre class="overflow-visible!" data-start="3493" data-end="3558"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Exposure</span><span></span><span>Margin</span><span></span><span>=</span><span> Animal AUC / Human AUC at Clinical Dose
</span></span></code></div></div></pre>

If exposure margin < 10× → highlight in **2.4.5 risk assessment** .

### **Dose-Limiting Toxicity Rule**

If a finding appears at:

- mid dose = important
- high dose only = supportive
- recovery-reversibility = reduced risk weight

These are algorithmically ranked.

### **Consistency Rules**

If a finding repeats across species → auto-flag in 2.4.5.

---

# **5. Section Assembly Logic**

After mapping, the CTD assembler constructs section text from building blocks.

Example:

### **2.6.6 Toxicity Summary → Template Assembly**

<pre class="overflow-visible!" data-start="4089" data-end="4244"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>[</span><span>Study Overview Paragraph</span><span>]
[</span><span>Findings by Organ System</span><span>]
[</span><span>NOAEL Table</span><span>]
[</span><span>Sex Differences Summary</span><span>]
[</span><span>Reversibility/Recovery Summary</span><span>]
[</span><span>Pathology Findings</span><span>]
</span></span></code></div></div></pre>

### **2.6.3 PK Summary**

<pre class="overflow-visible!" data-start="4271" data-end="4369"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>[</span><span>Absorption</span><span>]
[</span><span>Distribution</span><span>]
[</span><span>Metabolism</span><span>]
[</span><span>Excretion</span><span>]
[</span><span>Dose Proportionality</span><span>]
[</span><span>Accumulation</span><span>]
</span></span></code></div></div></pre>

### **2.4.5 Integrated Risk Assessment**

<pre class="overflow-visible!" data-start="4412" data-end="4533"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>[</span><span>Cross-Species Finding Integration</span><span>]
[</span><span>Exposure Margin Evaluation</span><span>]
[</span><span>Human Risk Implications</span><span>]
[</span><span>Overall Benefit-Risk</span><span>]
</span></span></code></div></div></pre>

---

# **6. Reference Linking**

Each CTD paragraph includes traceability tags:

<pre class="overflow-visible!" data-start="4616" data-end="4748"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span><</span><span>ref</span><span> doc</span><span>=</span><span>"0436RL35" section</span><span>=</span><span>"Clinical Chemistry" </span><span>start</span><span>=</span><span>"20433" </span><span>end</span><span>=</span><span>"20501"</span><span>></span><span>
ALT increased </span><span>at</span><span></span><span>150</span><span> mg</span><span>/</span><span>kg </span><span>in</span><span></span><span>both</span><span> sexes.
</span><span><</span><span>/</span><span>ref</span><span>></span><span>
</span></span></code></div></div></pre>

This enables UI highlighting of original text.

---

# **7. Data Structures**

## **7.1 Mapping Rules Table (`ctd_mapping_rules`)**

<pre class="overflow-visible!" data-start="4882" data-end="5019"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{</span><span>
  </span><span>"rule_id"</span><span>:</span><span></span><span>"NOAEL_TO_2.6.6"</span><span>,</span><span>
  </span><span>"topic"</span><span>:</span><span></span><span>"noael"</span><span>,</span><span>
  </span><span>"conditions"</span><span>:</span><span></span><span>{</span><span>"species"</span><span>:</span><span></span><span>"*"</span><span>}</span><span>,</span><span>
  </span><span>"targets"</span><span>:</span><span></span><span>[</span><span>"2.6.6"</span><span>,</span><span></span><span>"2.4.4"</span><span>,</span><span></span><span>"2.4.5"</span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

## **7.2 Mapped Output Table (`ctd_sections_generated`)**

<pre class="overflow-visible!" data-start="5079" data-end="5188"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{
  "</span><span>section</span><span>": </span><span>"2.6.6"</span><span>,
  </span><span>"content"</span><span>: </span><span>"... text ..."</span><span>,
  </span><span>"references"</span><span>: [...],
  </span><span>"document_ids"</span><span>: [...]
}
</span></span></code></div></div></pre>

---

# **8. Template Override Support**

Tenants can override:

- section order
- writing tone
- safety-margin thresholds
- merged vs separated species summaries

Example override:

<pre class="overflow-visible!" data-start="5370" data-end="5505"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{</span><span>
  </span><span>"tenant_id"</span><span>:</span><span></span><span>"AstraZeneca"</span><span>,</span><span>
  </span><span>"section"</span><span>:</span><span></span><span>"2.6.6"</span><span>,</span><span>
  </span><span>"template_override"</span><span>:</span><span></span><span>"{{species_block}} {{noael_block}} {{tk_block}}"</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# **9. Example End-to-End Mapping**

### **Extracted Data**

<pre class="overflow-visible!" data-start="5572" data-end="5673"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Finding:</span><span> ALT increased at </span><span>150</span><span> mg/kg
</span><span>Species:</span><span> Monkey
</span><span>Severity:</span><span> Moderate
</span><span>Reversibility:</span><span></span><span>Partial</span><span>
</span></span></code></div></div></pre>

### **Mapping Output**

<pre class="overflow-visible!" data-start="5698" data-end="5783"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>→ </span><span>2.6</span><span>.</span><span>6</span><span> Toxicity </span><span>Summary</span><span> 
→ </span><span>2.4</span><span>.</span><span>4</span><span> Toxicology Overview
→ </span><span>2.4</span><span>.</span><span>5</span><span> Risk Assessment
</span></span></code></div></div></pre>

### **Generated CTD Fragment**

<pre class="overflow-visible!" data-start="5816" data-end="5977"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Elevations </span><span>in</span><span> ALT were observed at 150 mg/kg </span><span>in</span><span> monkeys (moderate, partially reversible). 
These findings contributed to the identification of the NOAEL.
</span></span></code></div></div></pre>

---

# **10. Future Extensions**

- EMA/PMDA CTD variants
- Mapping to FDA 2024 new electronic submission format
- Multi-study weighted scoring models
- AI QA checker for mapping correctness
