**CTD Mapping System — Module 4 → CTD Module 2.4 / 2.6**

_(Based on your uploaded JSON mapping_ — `module4_to_26_mapping_complete.json` 0436rl35-001

\*)

📘 **Mappings.md**

---

## **1. Overview**

This document describes the complete mapping logic used to convert **Module 4 (Nonclinical Reports)** content into **Module 2.4 (Nonclinical Written and Tabulated Summaries)** and **Module 2.6 (Nonclinical Overview)** .

Your JSON file acts as the **source of truth** for:

- Section-to-section mapping
- Topic mapping
- Keyword-driven mapping
- Subtype mapping (toxicity, PK, safety pharmacology)

This document explains the mapping principles, lookup rules, scoring, and resolution flow used by the Mapping Engine (Option B rule-based system).

---

## **2. JSON Structure**

Your JSON file defines mappings in the following canonical structure:

<pre class="overflow-visible!" data-start="1005" data-end="1417"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"4.2.1.1"</span><span>:</span><span></span><span>{</span><span>
    </span><span>"section_title"</span><span>:</span><span></span><span>"Repeat-Dose Toxicity Study in Rats"</span><span>,</span><span>
    </span><span>"maps_to"</span><span>:</span><span></span><span>[</span><span>"2.6.6.1"</span><span>,</span><span></span><span>"2.6.7"</span><span>]</span><span>,</span><span>
    </span><span>"keywords"</span><span>:</span><span></span><span>[</span><span>"NOAEL"</span><span>,</span><span></span><span>"histopathology"</span><span>,</span><span></span><span>"toxicity"</span><span>,</span><span></span><span>"organ weight"</span><span>]</span><span>,</span><span>
    </span><span>"topics"</span><span>:</span><span></span><span>[</span><span>"Repeat Dose"</span><span>,</span><span></span><span>"Toxicity"</span><span>,</span><span></span><span>"Rats"</span><span>]</span><span>
  </span><span>}</span><span>,</span><span>
  </span><span>"4.3.2"</span><span>:</span><span></span><span>{</span><span>
    </span><span>"section_title"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
    </span><span>"maps_to"</span><span>:</span><span></span><span>[</span><span>"2.6.4"</span><span>]</span><span>,</span><span>
    </span><span>"keywords"</span><span>:</span><span></span><span>[</span><span>"safety pharm"</span><span>,</span><span></span><span>"respiration"</span><span>]</span><span>,</span><span>
    </span><span>"topics"</span><span>:</span><span></span><span>[</span><span>"Safety Pharmacology"</span><span>]</span><span>
  </span><span>}</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

### Required fields

| Field           | Description                                                |
| --------------- | ---------------------------------------------------------- |
| `section_title` | Human-readable title from Module 4                         |
| `maps_to`       | One or more CTD 2.4 / 2.6 target sections                  |
| `keywords`      | Terms that trigger mapping when found in extracted content |
| `topics`        | Higher-level classification categories                     |

---

## **3. Mapping Principles**

### **3.1 Direct Section Mapping**

If the JSON explicitly maps `"4.X.Y.Z" -> ["2.6.A", "2.4.B.C"]`, the mapping engine forwards all extracted text, tables, and structured results to those CTD sections.

Example:

<pre class="overflow-visible!" data-start="1983" data-end="2014"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>4.2.3 → → 2.6.6 / 2.6.7
</span></span></code></div></div></pre>

---

### **3.2 Topic-Based Mapping**

If content is classified (via LLM or rules) as:

| Topic                | Maps to CTD  |
| -------------------- | ------------ |
| Repeat dose toxicity | 2.6.6, 2.6.7 |
| Safety pharmacology  | 2.6.4        |
| Pharmacokinetics     | 2.6.5        |
| Single dose toxicity | 2.6.6.1      |
| Genotox              | 2.6.8        |

Topic classification influences ranking weights and resolves ambiguous mappings.

---

### **3.3 Keyword-Based Mapping**

Each mapping entry includes keywords such as:

<pre class="overflow-visible!" data-start="2478" data-end="2553"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>"NOAEL"</span><span>, </span><span>"Cmax"</span><span>, </span><span>"TK"</span><span>, </span><span>"organ weight"</span><span>, </span><span>"necropsy"</span><span>, </span><span>"histopathology"</span><span>
</span></span></code></div></div></pre>

The engine uses **keyword scoring** :

| Keyword found                           | Score |
| --------------------------------------- | ----- |
| exact match                             | +3    |
| stem match (e.g., "toxic" → "toxicity") | +2    |
| synonym match                           | +1    |

Mapping decision is a hybrid of:

<pre class="overflow-visible!" data-start="2773" data-end="2831"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>direct_section_score</span><span> + keyword_score + topic_score
</span></span></code></div></div></pre>

---

## **4. CTD Target Section Definitions**

### **2.4 — Nonclinical Summary**

- 2.4.1 Pharmacology
- 2.4.2 Pharmacokinetics
- 2.4.3 Toxicology Overview

### **2.6 — Nonclinical Overview**

- 2.6.1 Overview
- 2.6.2 Pharmacology
- 2.6.3 Pharmacokinetics
- 2.6.4 Safety Pharmacology
- 2.6.5 Pharmacokinetics Summary Tables
- 2.6.6 Toxicology
- 2.6.7 Integrated Discussion
- 2.6.8 Other Studies

The mapping system routes extracted Module 4 content into these sections.

---

## **5. Conflict Resolution Rules**

### ✔ **Rule 1 — Direct mapping wins**

If the JSON defines `"4.2.1.1" → 2.6.6`, that is always used unless overridden by tenant settings.

### ✔ **Rule 2 — Otherwise use highest scoring CTD section**

The system aggregates:

- LLM classification score
- Keyword match score
- Topic cluster score

### ✔ **Rule 3 — If multiple sections tie**

Send content to **both** sections (2.4 + 2.6), but with different weights:

- **2.4** receives summarized/synthesized content
- **2.6** receives tabulated + raw data summary

---

## **6. Multi-Level Mapping Flow**

<pre class="overflow-visible!" data-start="3929" data-end="4162"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Module</span><span></span><span>4</span><span></span><span>doc</span><span>
   ↓ (LLM </span><span>classifier</span><span>: PK / Tox / Safety Pharm / Other)
 </span><span>Section</span><span></span><span>ID</span><span></span><span>detected</span><span> (</span><span>regex</span><span>: </span><span>4</span><span>.X.Y)
   ↓
 </span><span>Lookup</span><span></span><span>in</span><span></span><span>JSON</span><span></span><span>mapping</span><span></span><span>file</span><span>
   ↓
 </span><span>Score</span><span></span><span>keywords</span><span> + </span><span>topics</span><span>
   ↓
 </span><span>Final</span><span></span><span>CTD</span><span></span><span>Target</span><span>(s)
   ↓
 </span><span>Save</span><span></span><span>mapping</span><span></span><span>trace</span><span></span><span>object</span><span>
</span></span></code></div></div></pre>

Trace object example:

<pre class="overflow-visible!" data-start="4187" data-end="4421"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"source"</span><span>:</span><span></span><span>"4.2.1.1"</span><span>,</span><span>
  </span><span>"resolved_to"</span><span>:</span><span></span><span>[</span><span>"2.6.6"</span><span>,</span><span></span><span>"2.6.7"</span><span>]</span><span>,</span><span>
  </span><span>"scores"</span><span>:</span><span></span><span>{</span><span>
    </span><span>"section_match"</span><span>:</span><span></span><span>1.0</span><span>,</span><span>
    </span><span>"keyword_score"</span><span>:</span><span></span><span>3.5</span><span>,</span><span>
    </span><span>"topic_score"</span><span>:</span><span></span><span>2.0</span><span>
  </span><span>}</span><span>,</span><span>
  </span><span>"reason"</span><span>:</span><span></span><span>"Direct mapping + NOAEL keyword + repeat-dose topic"</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

## **7. Integration With Ingestion Pipeline**

During PDF ingestion:

1. Extracted text →
2. Run structure detector →
3. Detect section numbers →
4. Classify by tox/PK/safety →
5. Extract NOAEL/TK table/Organ weights →
6. Resolve CTD mapping →
7. Store in `dynamic_dictionary_items` table →
8. Expose search keys (`topics`, `ctd_targets`, `module4_section`)

---

## **8. How Tenants Override Mapping**

Tenants may override:

- `maps_to`
- `keywords`
- `topics`
- entire mapping entries

Overrides stored in:

<pre class="overflow-visible!" data-start="4955" data-end="4991"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>tenant_ctd_mapping_overrides</span><span>
</span></span></code></div></div></pre>

Resolution priority:

<pre class="overflow-visible!" data-start="5015" data-end="5071"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>tenant </span><span>override</span><span> → </span><span>global</span><span></span><span>JSON</span><span> → inferred mapping
</span></span></code></div></div></pre>

---

## **9. Example Mapping Explanations**

### Example 1 — Repeat-Dose Toxicity

From JSON:

<pre class="overflow-visible!" data-start="5167" data-end="5203"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>4.2</span><span>.</span><span>1.2</span><span> → </span><span>["2.6.6"</span><span>, </span><span>"2.6.7"</span><span>]
</span></span></code></div></div></pre>

Extracted content contains:

- “NOAEL = 10 mg/kg”
- “organ weights”
- “microscopic findings”

Mapping engine resolves:

- 2.6.6 — toxicology results
- 2.6.7 — integrated discussion

---

### Example 2 — Toxicokinetics

From JSON:

<pre class="overflow-visible!" data-start="5439" data-end="5464"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>4.2</span><span>.X</span><span> → </span><span>["2.6.5"</span><span>]
</span></span></code></div></div></pre>

If TK parameters (`Cmax`, `AUC`, `T½`) are extracted:

- 2.6.5 receives tabulated TK summary
- 2.6.3 (PK summary) may also receive short text if classification confidence > threshold

---

### Example 3 — Safety Pharmacology

From JSON:

<pre class="overflow-visible!" data-start="5705" data-end="5774"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>keywords:</span><span> [</span><span>"respiratory"</span><span>, </span><span>"cardio"</span><span>, </span><span>"CNS"</span><span>]
</span><span>maps_to:</span><span> [</span><span>"2.6.4"</span><span>]
</span></span></code></div></div></pre>

Mapping engine resolves:

- 100% → 2.6.4 Safety Pharmacology

---

## **10. Searchability of Mappings**

The mapping system makes the following searchable keys:

| Searchable Key    | Example                            |
| ----------------- | ---------------------------------- |
| `module4_section` | "4.2.1.1"                          |
| `ctd_targets`     | ["2.6.6", "2.6.7"]                 |
| `topics`          | ["Repeat Dose", "Toxicity", "Rat"] |
| `keywords`        | ["NOAEL", "Cmax"]                  |

This enables:

- Fast UI autocomplete
- Query by topic (“show all TK sections”)
- Query by CTD section (“show all sources mapped to 2.6.6”)

---

## **11. Validation Rules**

### ✓ Ensure each Module 4 section maps to at least one CTD target

### ✓ Ensure no CTD section is missing mandatory study types

### ✓ Ensure toxicology studies always push content to 2.6.6 at minimum

### ✓ Ensure every mapping entry includes keywords and topics

---

## **12. Summary**

The mapping engine:

- Uses your JSON (0436rl35-001

  ) as the canonical definition

- Supports keyword + topic + rule-based scoring
- Handles ambiguous cases
- Produces deterministic CTD target mappings
- Integrates fully into your AI ingestion + summarization pipeline

This document describes everything needed to understand or modify the CTD mapping logic.
