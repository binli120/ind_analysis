# Prompts

# **LLM PROMPTS FOR IND INGESTION & CTD GENERATION**

This document defines all standardized prompts used in the IND ingestion engine — including document classification, parameter extraction, dictionary construction, and CTD section generation.

Each prompt is modular, deterministic, and optimized for regulatory-grade consistency.

---

# **1. Document Classification Prompts**

## **1.1 Study Type Classifier**

<pre class="overflow-visible!" data-start="613" data-end="1228"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>You are a regulatory nonclinical </span><span>study</span><span> classifier.

Given the extracted text, determine the correct </span><span>study</span><span> type:
- Repeat-dose GLP toxicity </span><span>study</span><span>
- Safety pharmacology </span><span>study</span><span>
- Pharmacokinetics (PK)
- Toxicokinetics (TK)
- Single-dose PK
- Analytical/bioanalytical report
- Dose formulation analysis
- Histopathology report
- Protocol </span><span>or</span><span> protocol amendment
- Deviation </span><span>or</span><span> QA report
- Immunogenicity / ADA assay
- Antibody quantification (ELISA/ECL)
- Other (specify)

Output JSON:
{
  </span><span>"study_type"</span><span>: </span><span>"..."</span><span>,
  </span><span>"species"</span><span>: </span><span>"..."</span><span>,
  </span><span>"route"</span><span>: </span><span>"..."</span><span>,
  </span><span>"duration"</span><span>: </span><span>"..."</span><span>,
  </span><span>"glp_status"</span><span>: </span><span>"..."</span><span>,
  </span><span>"confidence"</span><span>: </span><span>0</span><span>-</span><span>1</span><span>
}
</span></span></code></div></div></pre>

---

# **2. Section Extraction Prompts**

## **2.1 Hierarchical Section Detector**

<pre class="overflow-visible!" data-start="1313" data-end="1735"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>You are </span><span>a</span><span></span><span>section</span><span>-segmentation engine.

Identify </span><span>all</span><span></span><span>section</span><span> headers that match patterns:
- Numeric CTD-like sections (e.g., </span><span>4.2</span><span>, </span><span>4.2</span><span>.</span><span>1.3</span><span>)
- GLP study structure sections (e.g., </span><span>"VI. Materials and Methods"</span><span>)
- Repeated study-report patterns (</span><span>"Dose Formulation Analysis"</span><span>, </span><span>"Clinical Pathology"</span><span>)

Return JSON:
[
  {
    "section_number": </span><span>"..."</span><span>,
    </span><span>"section_title"</span><span>: </span><span>"..."</span><span>,
    </span><span>"start_idx"</span><span>: ...,
    </span><span>"end_idx"</span><span>: ...
  }
]
</span></span></code></div></div></pre>

---

# **3. Parameter Extraction Prompts**

## **3.1 NOAEL Extractor**

<pre class="overflow-visible!" data-start="1808" data-end="2192"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>You are </span><span>a</span><span> NOAEL extraction engine.

</span><span>From</span><span> the </span><span>text</span><span>, extract:
- Species-specific NOAEL
- Sex differences
- Dose values
- Basis for NOAEL (findings, organs affected)
- Whether a NOAEL could NOT be determined

Output JSON:
{
  "species": </span><span>"..."</span><span>,
  </span><span>"sex"</span><span>: </span><span>"..."</span><span>,
  </span><span>"noael_mg_per_kg"</span><span>: </span><span>"... or null"</span><span>,
  </span><span>"justification"</span><span>: </span><span>"..."</span><span>,
  </span><span>"findings_limiting_noael"</span><span>: [...],
  </span><span>"confidence"</span><span>: </span><span>0</span><span>-</span><span>1</span><span>
}
</span></span></code></div></div></pre>

---

## **3.2 Dose Group Table Extractor**

<pre class="overflow-visible!" data-start="2237" data-end="2478"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Extract </span><span>all</span><span> dose groups:

Return JSON:
{
  "dose_groups": [
    {
      "group_id": </span><span>1</span><span>,
      </span><span>"dose_mg_per_kg"</span><span>: ...,
      </span><span>"n_males"</span><span>: ...,
      </span><span>"n_females"</span><span>: ...,
      </span><span>"route"</span><span>: </span><span>"..."</span><span>,
      </span><span>"schedule"</span><span>: </span><span>"QD / weekly / etc"</span><span>
    }
  ]
}
</span></span></code></div></div></pre>

---

## **3.3 Toxicokinetic Parameter Extractor (TK)**

<pre class="overflow-visible!" data-start="2535" data-end="2898"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Extract</span><span></span><span>TK</span><span></span><span>parameters</span><span></span><span>(</span><span>Cmax</span><span>,</span><span></span><span>AUC</span><span>,</span><span></span><span>t1</span><span>/</span><span>2</span><span>,</span><span></span><span>CL</span><span>,</span><span></span><span>Vz</span><span>,</span><span></span><span>MRT</span><span>,</span><span></span><span>Tmax</span><span>)</span><span>.</span><span>

</span><span>If</span><span></span><span>multiple</span><span></span><span>days</span><span>:</span><span></span><span>produce</span><span></span><span>per</span><span>-</span><span>day</span><span></span><span>table</span><span>.</span><span>

</span><span>Return</span><span></span><span>JSON</span><span>:</span><span>
</span><span>{</span><span>
  </span><span>"tk_parameters"</span><span>:</span><span></span><span>[</span><span>
    </span><span>{</span><span>
      </span><span>"group"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"sex"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"day"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"cmax"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"auc"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"t_half"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"exposure_multiple"</span><span>:</span><span></span><span>"AUCDose / NOAEL TK"</span><span>,</span><span>
      </span><span>"comments"</span><span>:</span><span></span><span>"..."</span><span>
    </span><span>}</span><span>
  </span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

## **3.4 PK Parameter Extractor**

<pre class="overflow-visible!" data-start="2939" data-end="3099"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Extract pharmacokinetic interpretations:
</span><span>- absorption</span><span>
</span><span>- distribution</span><span>
</span><span>- clearance</span><span>
</span><span>- accumulation</span><span>
</span><span>- linearity / dose proportionality</span><span>

Return JSON summary.
</span></span></code></div></div></pre>

---

## **3.5 Findings Extractor**

<pre class="overflow-visible!" data-start="3136" data-end="3672"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Extract</span><span></span><span>findings</span><span></span><span>across</span><span>:</span><span>
</span><span>-</span><span></span><span>clinical</span><span></span><span>observations</span><span>
</span><span>-</span><span></span><span>body</span><span></span><span>weights</span><span>
</span><span>-</span><span></span><span>food</span><span></span><span>consumption</span><span>
</span><span>-</span><span></span><span>ophthalmology</span><span>
</span><span>-</span><span></span><span>ECG</span><span>
</span><span>-</span><span></span><span>clinical</span><span></span><span>chemistry</span><span>,</span><span></span><span>hematology</span><span>,</span><span></span><span>coagulation</span><span>
</span><span>-</span><span></span><span>urinalysis</span><span>
</span><span>-</span><span></span><span>organ</span><span></span><span>weights</span><span>
</span><span>-</span><span></span><span>gross</span><span></span><span>pathology</span><span>
</span><span>-</span><span></span><span>microscopic</span><span></span><span>pathology</span><span>

</span><span>Return</span><span></span><span>JSON</span><span>:</span><span>
</span><span>{</span><span>
  </span><span>"findings"</span><span>:</span><span></span><span>[</span><span>
    </span><span>{</span><span>
      </span><span>"category"</span><span>:</span><span></span><span>"clinical chemistry"</span><span>,</span><span>
      </span><span>"parameter"</span><span>:</span><span></span><span>"ALT"</span><span>,</span><span>
      </span><span>"direction"</span><span>:</span><span></span><span>"increase/decrease"</span><span>,</span><span>
      </span><span>"severity"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"dose_relationship"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"reversibility"</span><span>:</span><span></span><span>"..."</span><span>,</span><span>
      </span><span>"affected_groups"</span><span>:</span><span></span><span>[</span><span>...</span><span>]</span><span>,</span><span>
      </span><span>"interpretation"</span><span>:</span><span></span><span>"..."</span><span>
    </span><span>}</span><span>
  </span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

## **3.6 Histopathology Extractor**

<pre class="overflow-visible!" data-start="3715" data-end="3903"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Extract:
</span><span>- Organs affected</span><span>
</span><span>- Lesion types</span><span>
</span><span>- Severity (minimal/mild/moderate/severe)</span><span>
</span><span>- Incidence per sex</span><span>
</span><span>- Dose relationship</span><span>
</span><span>- Reversibility (if recovery group exists)</span><span>

Return JSON.
</span></span></code></div></div></pre>

---

# **4. Dictionary Construction Prompts**

## **4.1 Topic Extractor**

<pre class="overflow-visible!" data-start="3979" data-end="4133"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Identify </span><span>all</span><span> topics relevant </span><span>to</span><span> CTD </span><span>2.4</span><span></span><span>and</span><span></span><span>2.6</span><span>:
(e.g., NOAEL, TK exposure, histopathology, clinical chemistry)

</span><span>Return</span><span> list </span><span>of</span><span> normalized topics.
</span></span></code></div></div></pre>

## **4.2 Entity Extractor**

<pre class="overflow-visible!" data-start="4163" data-end="4306"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Find all biomedical entities:
</span><span>- organs</span><span>
</span><span>- analytes</span><span>
</span><span>- biomarkers</span><span>
</span><span>- dose values</span><span>
</span><span>- units</span><span>
</span><span>- parameters</span><span>

Return deduplicated normalized list.
</span></span></code></div></div></pre>

## **4.3 Section-to-Dictionary Mapping**

<pre class="overflow-visible!" data-start="4349" data-end="4501"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>For each extracted section, determine:
</span><span>- topic category</span><span>
</span><span>- CTD relevance</span><span>
</span><span>- how this section should be used in CTD summaries</span><span>

Return JSON mapping.
</span></span></code></div></div></pre>

---

# **5. CTD Generation Prompts**

## **5.1 CTD 2.6 Section Generator**

<pre class="overflow-visible!" data-start="4578" data-end="4939"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>You are generating ICH CTD Module </span><span>2.6</span><span></span><span>text</span><span>.

Inputs:
- structured parameters (NOAEL, TK, findings)
- dictionary topics
- extracted tables
- study metadata

Write a factual, concise nonclinical summary.
Use regulatory tone.
Include no hallucinations.

Return:
{
  "</span><span>text</span><span>": </span><span>"..."</span><span>,
  </span><span>"references"</span><span>: [
    { "source_section": </span><span>"..."</span><span>, </span><span>"text_offset"</span><span>: ... }
  ]
}
</span></span></code></div></div></pre>

---

## **5.2 CTD 2.4 Overview Generator**

<pre class="overflow-visible!" data-start="4984" data-end="5285"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Generate the Nonclinical Overview (</span><span>2.4</span><span>) based </span><span>on</span><span> all nonclinical study data.

</span><span>Include:</span><span>
- overall risk assessment
- weight </span><span>of</span><span> evidence across all tox, TK, PK, safety pharm studies
- exposure margins relative </span><span>to</span><span> clinical dose

Output must be structured, regulatory, </span><span>and</span><span> traceable </span><span>to</span><span> source data.
</span></span></code></div></div></pre>

---

## **5.3 Tenant-Level Custom Template Loader**

<pre class="overflow-visible!" data-start="5339" data-end="5583"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>Given a tenant-defined template </span><span>and</span><span> the extracted data,
produce CTD output respecting:

- </span><span>custom</span><span> tone
- </span><span>custom</span><span> ordering
- </span><span>custom</span><span> table formats
- </span><span>custom</span><span> risk-language preferences

Fill </span><span>with</span><span> extracted values only. No invented information.
</span></span></code></div></div></pre>

---

# **6. Safety & Compliance Guards**

## **6.1 Hallucination Prevention Layer**

<pre class="overflow-visible!" data-start="5669" data-end="5873"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>If required data is missing, say so explicitly.

Do NOT invent:
</span><span>- doses</span><span>
</span><span>- NOAELs</span><span>
</span><span>- TK parameters</span><span>
</span><span>- findings</span><span>
</span><span>- organ weights</span><span>
</span><span>- sample sizes</span><span>

Always check: “Is this value present in the input text?”
</span></span></code></div></div></pre>

---

## **6.2 Missing Data Reporter**

<pre class="overflow-visible!" data-start="5913" data-end="6017"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>List </span><span>all</span><span></span><span>missing</span><span> regulatory fields required </span><span>for</span><span> CTD </span><span>2.4</span><span>/</span><span>2.6</span><span>.

Return JSON </span><span>list</span><span> of </span><span>missing</span><span> items.
</span></span></code></div></div></pre>

---

# **7. Prompt Style Rules (Global)**

- All outputs must be **deterministic** , JSON-structured when requested.
- No marketing language.
- No excessive speculation.
- Reference source study text explicitly whenever possible.
- Prefer shorter sentences and bullet points for regulatory readability.
- Use standard toxicology terminology.

---

# **8. Prompt Versioning Convention**

- Store prompts in DB table: `llm_prompts`
- Version naming:

  `classifier:v1`

  `noael_extractor:v2`

  `ctd_2_6_generator:v3`

- Each tenant may override specific prompt versions.

---

# **9. Override / Customization Support**

Each prompt may accept runtime overrides:

<pre class="overflow-visible!" data-start="6697" data-end="6870"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>{
  </span><span>"temperature"</span><span>: </span><span>0.0</span><span>–</span><span>1.0</span><span>,
  </span><span>"style"</span><span>: </span><span>"concise | verbose | FDA-strict | EMA-style"</span><span>,
  </span><span>"language"</span><span>: </span><span>"en"</span><span>,
  </span><span>"template_override"</span><span>: </span><span>"..."</span><span>,
  </span><span>"excluded_sections"</span><span>: [...]
}</span></span></code></div></div></pre>
