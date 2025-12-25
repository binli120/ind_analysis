"""Fast extraction helpers driven by CTD template hints."""

# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

FAST_FIELD = "Fast extraction method (keywords/tables/regex)"
_SECTION_RE = re.compile(r"^2\.\d+(?:\.\d+)*$")

_FAST_INDEX: Dict[str, "FastExtractRule"] | None = None


@dataclass
class FastExtractRule:
    section: str
    keywords: List[str]
    table_cues: List[str]
    regex_patterns: List[str]
    compiled_regex: List[re.Pattern[str]]


def _template_paths() -> List[Path]:
    base = Path(__file__).resolve().parents[2]
    return [
        base / "summary" / "ind_24_26_template.json",
        base / "ncd" / "ind_24_26_template.json",
    ]


def _load_template_entries() -> List[dict]:
    for path in _template_paths():
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries: List[dict] = []
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    entries.extend(item for item in value if isinstance(item, dict))
        return entries
    return []


def _split_terms(raw: str) -> List[str]:
    cleaned = raw.replace(";", ",")
    parts = [p.strip().strip('"').strip("'") for p in cleaned.split(",")]
    return [p for p in parts if p]


def _parse_fast_method(text: str) -> tuple[List[str], List[str], List[str]]:
    keywords: List[str] = []
    table_cues: List[str] = []
    regex_patterns: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if "keyword" in lower:
            _, _, remainder = line.partition(":")
            keywords.extend(_split_terms(remainder))
            continue
        if "table cue" in lower:
            _, _, remainder = line.partition(":")
            table_cues.extend(_split_terms(remainder))
            continue
        if "regex" in lower:
            _, _, remainder = line.partition(":")
            pattern = remainder.strip()
            if pattern:
                regex_patterns.append(pattern)
            continue
    return keywords, table_cues, regex_patterns


def _compile_patterns(patterns: Sequence[str]) -> List[re.Pattern[str]]:
    compiled: List[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def _normalize_section(entry: dict) -> Optional[str]:
    subsection = str(entry.get("Subsection") or "").strip()
    section = str(entry.get("Section") or "").strip()
    if subsection and _SECTION_RE.match(subsection):
        return subsection
    if section and _SECTION_RE.match(section):
        return section
    return None


def build_fast_extract_index() -> Dict[str, FastExtractRule]:
    global _FAST_INDEX
    if _FAST_INDEX is not None:
        return _FAST_INDEX

    index: Dict[str, FastExtractRule] = {}
    for entry in _load_template_entries():
        section = _normalize_section(entry)
        if not section:
            continue
        raw = entry.get(FAST_FIELD)
        if not isinstance(raw, str) or not raw.strip():
            continue
        keywords, table_cues, regex_patterns = _parse_fast_method(raw)
        if not (keywords or table_cues or regex_patterns):
            continue
        existing = index.get(section)
        if existing:
            existing.keywords.extend(k for k in keywords if k not in existing.keywords)
            existing.table_cues.extend(c for c in table_cues if c not in existing.table_cues)
            existing.regex_patterns.extend(
                p for p in regex_patterns if p not in existing.regex_patterns
            )
            existing.compiled_regex = _compile_patterns(existing.regex_patterns)
            continue
        index[section] = FastExtractRule(
            section=section,
            keywords=keywords,
            table_cues=table_cues,
            regex_patterns=regex_patterns,
            compiled_regex=_compile_patterns(regex_patterns),
        )

    _FAST_INDEX = index
    return _FAST_INDEX


def match_fast_extract(
    text: str,
    *,
    section_prefixes: Optional[Sequence[str]] = None,
    extra_texts: Optional[Iterable[str]] = None,
) -> List[Dict[str, List[str]]]:
    combined = " ".join(filter(None, [text, *(extra_texts or [])]))
    if not combined.strip():
        return []
    lowered = combined.lower()
    prefixes = tuple(section_prefixes or [])

    matches: List[Dict[str, List[str]]] = []
    for section, rule in build_fast_extract_index().items():
        if prefixes and not section.startswith(prefixes):
            continue
        matched_keywords = [kw for kw in rule.keywords if kw.lower() in lowered]
        matched_regex = [pat for pat in rule.regex_patterns if _regex_hits(rule, pat, combined)]
        matched_table = [kw for kw in rule.table_cues if kw.lower() in lowered]
        if matched_keywords or matched_regex or matched_table:
            matches.append(
                {
                    "section": section,
                    "keywords": matched_keywords,
                    "table_cues": matched_table,
                    "regex": matched_regex,
                }
            )
    return matches


def _regex_hits(rule: FastExtractRule, pattern: str, text: str) -> bool:
    for compiled in rule.compiled_regex:
        if compiled.pattern == pattern and compiled.search(text):
            return True
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        return False
