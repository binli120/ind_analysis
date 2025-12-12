# **CONFIGURATION.md**

<pre class="overflow-visible!" data-start="295" data-end="814"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-markdown"><span><span># Configuration Guide</span><span>
IND AI Platform — User, Tenant, and System-Level Configuration

This document explains how configuration works across the IND AI platform.  
It describes:

</span><span>-</span><span> User-level AI settings  
</span><span>-</span><span> Tenant-level defaults  
</span><span>-</span><span> System-wide fallbacks  
</span><span>-</span><span> How prompts, temperatures, dictionary terms, and mapping rules cascade  
</span><span>-</span><span> How configuration is stored, updated, and applied inside pipelines  

---

</span><span># 1. Configuration Model Overview</span><span>

The system uses a </span><span>**three-layer configuration hierarchy**</span><span>:

</span></span></code></div></div></pre>

System Default

→ Tenant Configuration

→ User Configuration

<pre class="overflow-visible!" data-start="887" data-end="1897"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>
</span><span>Rules:</span><span>
</span><span>-</span><span></span><span>User</span><span></span><span>config</span><span></span><span>overrides</span><span></span><span>tenant</span><span></span><span>config.</span><span>
</span><span>-</span><span></span><span>Tenant</span><span></span><span>config</span><span></span><span>overrides</span><span></span><span>system</span><span></span><span>defaults.</span><span>
</span><span>-</span><span></span><span>Missing</span><span></span><span>values</span><span></span><span>inherit</span><span></span><span>automatically.</span><span>

</span><span>This allows flexibility across:</span><span>
</span><span>-</span><span></span><span>Individual</span><span></span><span>scientists</span><span>  
</span><span>-</span><span></span><span>Multi-project</span><span></span><span>biotech</span><span></span><span>teams</span><span>  
</span><span>-</span><span></span><span>CROs</span><span>  
</span><span>-</span><span></span><span>Enterprise</span><span></span><span>deployments</span><span>  

---

</span><span># 2. Configuration Categories</span><span>

</span><span>## 2.1 LLM Settings</span><span>
</span><span>Stored under each user or tenant:</span><span>

</span><span>-</span><span></span><span>`temperature`</span><span>  
</span><span>-</span><span></span><span>`model_name`</span><span>  
</span><span>-</span><span></span><span>`max_tokens`</span><span>  
</span><span>-</span><span></span><span>`json_mode`</span><span>  
</span><span>-</span><span></span><span>retry</span><span></span><span>and</span><span></span><span>timeout</span><span></span><span>policies</span><span>  

</span><span>Used in:</span><span>
</span><span>-</span><span></span><span>study</span><span></span><span>classification</span><span>  
</span><span>-</span><span></span><span>extraction</span><span></span><span>passes</span><span>  
</span><span>-</span><span></span><span>CTD</span><span></span><span>2.4</span><span></span><span>/</span><span></span><span>2.6</span><span></span><span>narrative</span><span></span><span>generation</span><span>  

---

</span><span>## 2.2 Extraction Prompt Configuration</span><span>

</span><span>Users or tenants may override prompts for:</span><span>

</span><span>-</span><span></span><span>NOAEL</span><span></span><span>extraction</span><span>  
</span><span>-</span><span></span><span>PK</span><span></span><span>parameter</span><span></span><span>extraction</span><span></span><span>(Cmax,</span><span></span><span>AUC,</span><span></span><span>Tmax,</span><span></span><span>T1/2)</span><span>  
</span><span>-</span><span></span><span>Findings</span><span></span><span>extraction</span><span>  
</span><span>-</span><span></span><span>Toxicology</span><span></span><span>organ</span><span></span><span>system</span><span></span><span>summaries</span><span>  
</span><span>-</span><span></span><span>TK</span><span></span><span>→</span><span></span><span>PK</span><span></span><span>mapping</span><span>  
</span><span>-</span><span></span><span>Table</span><span></span><span>normalization</span><span>  

</span><span>Example override structure:</span><span>

</span><span>```json</span><span>
{
  </span><span>"extract_noael":</span><span></span><span>"Your custom extraction instruction..."</span><span>,
  </span><span>"extract_pk":</span><span></span><span>"Your custom PK extraction instruction..."</span><span>,
  </span><span>"extract_findings":</span><span></span><span>"..."</span><span>
}
</span></span></code></div></div></pre>

If a prompt is missing, the system uses tenant defaults → system defaults.

---

## 2.3 CTD Mapping Configuration

Controls how extracted data maps to:

- CTD 2.4 Nonclinical Overview
- CTD 2.6.1 Pharmacokinetics
- CTD 2.6.2 Pharmacology
- CTD 2.6.3 Toxicology (repeat-dose toxicity, safety pharm, etc.)

Configuration includes:

- section mapping rules
- narrative templates
- citation format
- merge logic for multiple studies
- default paragraph ordering

Example:

<pre class="overflow-visible!" data-start="2383" data-end="2526"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"2.6.6.3"</span><span>:</span><span></span><span>{</span><span>
    </span><span>"template"</span><span>:</span><span></span><span>"Summarize repeat-dose toxicity findings..."</span><span>,</span><span>
    </span><span>"evidence_policy"</span><span>:</span><span></span><span>"top_confidence_chunks"</span><span>
  </span><span>}</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

## 2.4 Dynamic Dictionary Configuration

Tenant-wide dictionary terms may include:

- organ synonyms ("hepatic", "liver", "ALT")
- PK synonyms ("AUC0-24", "AUC(0-24)", "area under curve")
- toxicity keywords
- common CTD headings

Users can customize dictionary terms for:

- special drug modalities
- proprietary biomarkers
- novel endpoints

Dictionary entries are stored as:

<pre class="overflow-visible!" data-start="2925" data-end="3080"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"hepatic"</span><span>:</span><span></span><span>[</span><span>"ALT"</span><span>,</span><span></span><span>"AST"</span><span>,</span><span></span><span>"bilirubin"</span><span>,</span><span></span><span>"centrilobular"</span><span>]</span><span>,</span><span>
  </span><span>"kidney"</span><span>:</span><span></span><span>[</span><span>"BUN"</span><span>,</span><span></span><span>"creatinine"</span><span>,</span><span></span><span>"glomerulus"</span><span>]</span><span>,</span><span>
  </span><span>"pk"</span><span>:</span><span></span><span>[</span><span>"Cmax"</span><span>,</span><span></span><span>"AUC"</span><span>,</span><span></span><span>"t1/2"</span><span>]</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# 3. Database Schema for Configuration

## 3.1 `user_configurations`

| Column                      | Type      | Notes                      |
| --------------------------- | --------- | -------------------------- |
| id                          | UUID      | PK                         |
| user_id                     | UUID      | FK → users                 |
| temperature                 | float     | LLM parameter              |
| system_prompt               | text      | Global override            |
| extraction_prompt_overrides | jsonb     | Dict of override prompts   |
| ctd_prompt_overrides        | jsonb     | CTD-level prompt overrides |
| created_at                  | timestamp |                            |

---

## 3.2 `tenant_configurations`

| Column           | Type        |
| ---------------- | ----------- |
| id               | UUID        |
| tenant_id        | UUID        |
| default_prompts  | jsonb       |
| dictionary_terms | jsonb       |
| created_at       | timestamptz |

---

## 3.3 System Defaults (Hardcoded or .env)

Fallback values live in:

<pre class="overflow-visible!" data-start="4002" data-end="4035"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>config/system_defaults.py
</span></span></code></div></div></pre>

Example:

<pre class="overflow-visible!" data-start="4047" data-end="4203"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-python"><span><span>SYSTEM_DEFAULTS = {
    </span><span>"temperature"</span><span>: </span><span>0.2</span><span>,
    </span><span>"model_name"</span><span>: </span><span>"gpt-4.1"</span><span>,
    </span><span>"max_tokens"</span><span>: </span><span>4096</span><span>,
    </span><span>"ctd_templates_path"</span><span>: </span><span>"templates/ctd/"</span><span>,
}
</span></span></code></div></div></pre>

---

# 4. How Configuration is Merged (Resolution Logic)

When any component requests config (e.g., LLM extraction), the resolver runs:

<pre class="overflow-visible!" data-start="4342" data-end="4447"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>config</span><span> = system_default
</span><span>config</span><span> = merge(config, tenant_config)
</span><span>config</span><span> = merge(config, user_config)
</span></span></code></div></div></pre>

Merging rules:

- dictionaries merge deeply
- `None` values do not overwrite
- user-level values always take precedence

Example:

<pre class="overflow-visible!" data-start="4585" data-end="4842"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-python"><span><span>resolved.temperature       = user.temperature </span><span>or</span><span> tenant.temperature </span><span>or</span><span> default.temperature
resolved.noael_prompt      = user.override.noael </span><span>or</span><span> tenant.override.noael </span><span>or</span><span> default.noael
resolved.dictionary_terms  = merge(default.</span><span>dict</span><span>, tenant.</span><span>dict</span><span>)
</span></span></code></div></div></pre>

---

# 5. API Endpoints for Configuration

## 5.1 GET Current User Config

<pre class="overflow-visible!" data-start="4919" data-end="4943"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>GET</span><span></span><span>/</span><span>config</span><span>/</span><span>user</span><span>
</span></span></code></div></div></pre>

## 5.2 Update User Config

<pre class="overflow-visible!" data-start="4972" data-end="5062"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>POST /config/user
{
  </span><span>"temperature"</span><span>: </span><span>0.1</span><span>,
  </span><span>"extraction_prompt_overrides"</span><span>: {...}
}
</span></span></code></div></div></pre>

## 5.3 Get Tenant Config (admin only)

<pre class="overflow-visible!" data-start="5103" data-end="5129"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>GET /config/tenant
</span></span></code></div></div></pre>

## 5.4 Update Tenant Config (admin only)

<pre class="overflow-visible!" data-start="5173" data-end="5200"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre!"><span><span>POST /config/tenant
</span></span></code></div></div></pre>

---

# 6. How Config Integrates with the Pipeline

| Pipeline Step        | Config Used                                   |
| -------------------- | --------------------------------------------- |
| PDF splitting        | dictionary terms (section detection synonyms) |
| study classification | prompts + temp                                |
| extraction engine    | extraction overrides                          |
| PK/TK extraction     | PK-specific overrides                         |
| dictionary builder   | tenant-level dictionary config                |
| CTD mapping          | CTD prompt overrides                          |
| final generation     | user overrides > tenant defaults              |

---

# 7. Configuration Examples

### Example 1 — User overrides only temperature

<pre class="overflow-visible!" data-start="5750" data-end="5787"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"temperature"</span><span>:</span><span></span><span>0.05</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

### Example 2 — Tenant defines dictionary

<pre class="overflow-visible!" data-start="5832" data-end="5950"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"dictionary_terms"</span><span>:</span><span></span><span>{</span><span>
    </span><span>"cardiac"</span><span>:</span><span></span><span>[</span><span>"ECG"</span><span>,</span><span></span><span>"QTc"</span><span>,</span><span></span><span>"arrhythmia"</span><span>]</span><span>,</span><span>
    </span><span>"hepatic"</span><span>:</span><span></span><span>[</span><span>"ALT"</span><span>,</span><span></span><span>"AST"</span><span>]</span><span>
  </span><span>}</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

### Example 3 — CTD template override

<pre class="overflow-visible!" data-start="5991" data-end="6112"><div class="contain-inline-size rounded-2xl corner-superellipse/1.1 relative bg-token-sidebar-surface-primary"><div class="sticky top-9"><div class="absolute end-0 bottom-0 flex h-9 items-center pe-2"><div class="bg-token-bg-elevated-secondary text-token-text-secondary flex items-center gap-4 rounded-sm px-2 font-sans text-xs"></div></div></div><div class="overflow-y-auto p-4" dir="ltr"><code class="whitespace-pre! language-json"><span><span>{</span><span>
  </span><span>"ctd_prompt_overrides"</span><span>:</span><span></span><span>{</span><span>
    </span><span>"2.6.6.3"</span><span>:</span><span></span><span>"Use a concise summary format for repeat-dose toxicity..."</span><span>
  </span><span>}</span><span>
</span><span>}</span><span>
</span></span></code></div></div></pre>

---

# 8. Security & Audit Behavior

- All config writes are logged in `audit_log`
- Role-based controls ensure:
  - users may update only their own config
  - tenant admins may update tenant defaults
- Version snapshots allow rollback to earlier config states

---

# 9. Future Extensions

- Per-project configuration layers
- Different LLM models per section (2.6 vs 2.4)
- Prompt versioning with tracking
- Auto-suggestion of dictionary terms from new data
