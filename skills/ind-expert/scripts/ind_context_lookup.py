#!/usr/bin/env python3
"""Resolve IND context from template + mappings for a target CTD section."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List


ELEMENT_RE = re.compile(r"^(2\.4(?:\.\d+)*)(?:-)?([a-z])$", re.IGNORECASE)


def normalize_element(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.strip().lower().replace(" ", "")
    match = ELEMENT_RE.match(cleaned)
    if match:
        return f"{match.group(1)}-{match.group(2).lower()}"
    return cleaned


def load_template_entries(path: Path) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        rows: List[Dict[str, Any]] = []
        for value in payload.values():
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
        return rows
    return []


def find_template_matches(
    entries: Iterable[Dict[str, Any]],
    section: str,
    element: str | None = None,
) -> List[Dict[str, Any]]:
    normalized_element = normalize_element(element)
    matches: List[Dict[str, Any]] = []
    for entry in entries:
        entry_section = str(entry.get("Subsection") or entry.get("Section") or "").strip()
        raw_element = str(entry.get("Subsection Element Numbering") or "").strip()
        entry_element = normalize_element(raw_element)
        if section and not (
            entry_section == section
            or raw_element.startswith(f"{section} ")
            or entry_element.startswith(section)
        ):
            continue
        if normalized_element and entry_element != normalized_element:
            continue
        matches.append(entry)
    return matches


def find_section_tree_matches(nodes: Iterable[Dict[str, Any]], section: str) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for node in nodes:
        value = str(node.get("value") or "").strip()
        if value == section or value.startswith(f"{section}."):
            output.append(node)
    return output


def _iter_module4_entries(raw_mapping: Dict[str, Any]) -> Iterable[tuple[str, Dict[str, Any]]]:
    for group_name in ("pharmacology_mappings", "pharmacokinetics_mappings", "toxicology_mappings"):
        group = raw_mapping.get(group_name, {})
        if not isinstance(group, dict):
            continue
        for module4_section, entry in group.items():
            if isinstance(entry, dict):
                yield module4_section, entry


def resolve_target_sections(section: str) -> List[str]:
    cleaned = (section or "").strip()
    if not cleaned:
        return []
    if cleaned.startswith("2.6"):
        return [cleaned]
    if cleaned.startswith("2.4.1"):
        return ["2.6"]
    if cleaned.startswith("2.4.2"):
        return ["2.6.2"]
    if cleaned.startswith("2.4.3"):
        return ["2.6.4"]
    if cleaned.startswith("2.4.4"):
        return ["2.6.6"]
    if cleaned.startswith("2.4.5"):
        return ["2.6.6", "2.6.7"]
    if cleaned.startswith("2.4.6"):
        return ["2.6.7"]
    return [cleaned]


def find_module4_mapping_matches(raw_mapping: Dict[str, Any], section: str) -> List[Dict[str, Any]]:
    resolved_targets = resolve_target_sections(section)
    matches: List[Dict[str, Any]] = []
    for module4_section, entry in _iter_module4_entries(raw_mapping):
        entry_target_sections = entry.get("target_sections") or {}
        if not isinstance(entry_target_sections, dict):
            continue
        written = [str(s) for s in entry_target_sections.get("written", []) if str(s).strip()]
        tabulated = [str(s) for s in entry_target_sections.get("tabulated", []) if str(s).strip()]
        all_targets = written + tabulated
        if not any(
            target == expected or target.startswith(f"{expected}.")
            for target in all_targets
            for expected in resolved_targets
        ):
            continue
        matches.append(
            {
                "module4_section": module4_section,
                "category": entry.get("category"),
                "priority": entry.get("priority"),
                "targets": {"written": written, "tabulated": tabulated},
                "required_content": entry.get("required_content", []),
                "resolved_section_targets": resolved_targets,
            }
        )
    return matches


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Lookup section context across IND template and mapping files."
    )
    parser.add_argument("--section", required=True, help="Target section, e.g. 2.4.2")
    parser.add_argument(
        "--element",
        help="Optional subsection element, e.g. 2.4.2-b (or '2.4.2 - b').",
    )
    parser.add_argument(
        "--template",
        default=str(repo_root / "src/ncd/ind_24_26_template.json"),
        help="Path to ind_24_26_template.json",
    )
    parser.add_argument(
        "--section-mapping",
        default="/Users/blee/dev/bin/ind-manager/config/section_List_mapping.json",
        help="Path to section_List_mapping.json",
    )
    parser.add_argument(
        "--module4-mapping",
        default=str(repo_root / "src/ncd/mapping/module4_to_26_mapping_complete.json"),
        help="Path to module4_to_26_mapping_complete.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    section = str(args.section).strip()
    element = args.element

    template_path = Path(args.template).expanduser()
    section_mapping_path = Path(args.section_mapping).expanduser()
    module4_mapping_path = Path(args.module4_mapping).expanduser()

    warnings: List[str] = []
    output: Dict[str, Any] = {
        "section": section,
        "element": normalize_element(element) if element else None,
        "paths": {
            "template": str(template_path),
            "section_mapping": str(section_mapping_path),
            "module4_mapping": str(module4_mapping_path),
        },
        "template_matches": [],
        "section_tree_matches": [],
        "module4_mapping_matches": [],
        "warnings": warnings,
    }

    if template_path.exists():
        template_entries = load_template_entries(template_path)
        output["template_matches"] = find_template_matches(template_entries, section, element)
    else:
        warnings.append(f"Template file not found: {template_path}")

    if section_mapping_path.exists():
        section_nodes = json.loads(section_mapping_path.read_text(encoding="utf-8"))
        if isinstance(section_nodes, list):
            output["section_tree_matches"] = find_section_tree_matches(section_nodes, section)
        else:
            warnings.append("Section mapping payload is not a JSON array.")
    else:
        warnings.append(f"Section mapping file not found: {section_mapping_path}")

    if module4_mapping_path.exists():
        module4_mapping_raw = json.loads(module4_mapping_path.read_text(encoding="utf-8"))
        if isinstance(module4_mapping_raw, dict):
            output["module4_mapping_matches"] = find_module4_mapping_matches(module4_mapping_raw, section)
        else:
            warnings.append("Module 4 mapping payload is not a JSON object.")
    else:
        warnings.append(f"Module 4 mapping file not found: {module4_mapping_path}")

    output["counts"] = {
        "template_matches": len(output["template_matches"]),
        "section_tree_matches": len(output["section_tree_matches"]),
        "module4_mapping_matches": len(output["module4_mapping_matches"]),
    }

    print(json.dumps(output, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
