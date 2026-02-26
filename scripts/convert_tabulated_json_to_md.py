#!/usr/bin/env python3
"""Convert CTD tabulated JSON payloads to Markdown with tables."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple


def _repair_invalid_escapes(raw: str) -> str:
    """Repair invalid JSON escapes by escaping unsupported backslashes inside strings."""
    out: List[str] = []
    in_string = False
    i = 0
    length = len(raw)
    while i < length:
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if ch == "\\":
            if i + 1 >= length:
                out.append("\\\\")
                i += 1
                continue
            nxt = raw[i + 1]
            if nxt in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
                out.append("\\")
                out.append(nxt)
                i += 2
                continue
            if (
                nxt == "u"
                and i + 5 < length
                and all(c in "0123456789abcdefABCDEF" for c in raw[i + 2 : i + 6])
            ):
                out.append(raw[i : i + 6])
                i += 6
                continue
            out.append("\\\\")
            i += 1
            continue

        out.append(ch)
        if ch == '"':
            in_string = False
        i += 1
    return "".join(out)


def _load_json_with_repair(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = json.loads(_repair_invalid_escapes(text))
    if not isinstance(payload, dict):
        raise ValueError("Expected top-level JSON object.")
    return payload


def _escape_md_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "<br>")
    text = text.replace("|", "\\|")
    return text.strip()


def _render_markdown_table(columns: Sequence[str], rows: Sequence[Dict[str, Any]]) -> List[str]:
    if not columns:
        return ["_No columns provided._"]
    header = "| " + " | ".join(_escape_md_cell(col) for col in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, divider]
    if not rows:
        lines.append("| " + " | ".join("" for _ in columns) + " |")
        return lines
    for row in rows:
        line = "| " + " | ".join(_escape_md_cell(row.get(col, "")) for col in columns) + " |"
        lines.append(line)
    return lines


def _clean_rows(
    columns: Sequence[str],
    rows: Sequence[Dict[str, Any]],
    drop_empty_rows: bool,
    dedupe_rows: bool,
    dedupe_by_columns: Sequence[str],
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    cleaned: List[Dict[str, Any]] = []
    seen: set[Tuple[str, ...]] = set()
    seen_keyed: set[Tuple[str, ...]] = set()
    dropped_empty = 0
    dropped_duplicate = 0
    dropped_key_duplicate = 0
    dedupe_by_columns_set = {name.strip().lower() for name in dedupe_by_columns if name.strip()}

    def normalize_key(text: str) -> str:
        compact = re.sub(r"\s+", "", text.strip().lower())
        return re.sub(r"[^a-z0-9]", "", compact)

    for row in rows:
        if not isinstance(row, dict):
            continue
        signature = tuple(
            str(row.get(col, "") if row.get(col, "") is not None else "").strip()
            for col in columns
        )

        if drop_empty_rows and all(cell == "" for cell in signature):
            dropped_empty += 1
            continue
        if dedupe_rows and signature in seen:
            dropped_duplicate += 1
            continue

        keyed_signature: Tuple[str, ...] | None = None
        if dedupe_by_columns_set:
            key_values: List[str] = []
            matched = False
            for col in columns:
                if col.strip().lower() in dedupe_by_columns_set:
                    matched = True
                    key_values.append(
                        normalize_key(
                            str(row.get(col, "") if row.get(col, "") is not None else "")
                        )
                    )
            if matched and any(v != "" for v in key_values):
                keyed_signature = tuple(key_values)
                if keyed_signature in seen_keyed:
                    dropped_key_duplicate += 1
                    continue

        seen.add(signature)
        if keyed_signature is not None:
            seen_keyed.add(keyed_signature)
        cleaned.append(row)

    return cleaned, dropped_empty, dropped_duplicate, dropped_key_duplicate


def _render_md(
    payload: Dict[str, Any],
    source_path: Path,
    drop_empty_rows: bool,
    dedupe_rows: bool,
    dedupe_by_columns: Sequence[str],
) -> str:
    lines: List[str] = []
    lines.append(f"# CTD Tabulated Summary ({_escape_md_cell(payload.get('section', 'Unknown'))})")
    lines.append("")
    lines.append(f"- Source JSON: `{source_path}`")
    if payload.get("tabulated_id"):
        lines.append(f"- Tabulated ID: `{payload['tabulated_id']}`")
    if payload.get("status"):
        lines.append(f"- Status: `{payload['status']}`")
    lines.append("")

    tables = payload.get("tables") or []
    if not isinstance(tables, list):
        tables = []

    if not tables:
        lines.append("_No tables found in payload._")
        return "\n".join(lines) + "\n"

    total_dropped_empty = 0
    total_dropped_duplicate = 0
    total_dropped_key_duplicate = 0

    for table in tables:
        if not isinstance(table, dict):
            continue
        subsection = str(table.get("subsection") or "").strip()
        header = str(table.get("subsection_header") or "").strip()
        title = " ".join(part for part in (subsection, header) if part).strip()
        lines.append(f"## {title or 'Table'}")
        lines.append("")
        description = str(table.get("description") or "").strip()
        if description:
            lines.append(description)
            lines.append("")

        columns = table.get("columns") or []
        if not isinstance(columns, list):
            columns = []
        columns = [str(col) for col in columns if str(col).strip()]

        rows = table.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        normalized_rows = [row for row in rows if isinstance(row, dict)]
        normalized_rows, dropped_empty, dropped_duplicate, dropped_key_duplicate = _clean_rows(
            columns=columns,
            rows=normalized_rows,
            drop_empty_rows=drop_empty_rows,
            dedupe_rows=dedupe_rows,
            dedupe_by_columns=dedupe_by_columns,
        )
        total_dropped_empty += dropped_empty
        total_dropped_duplicate += dropped_duplicate
        total_dropped_key_duplicate += dropped_key_duplicate

        lines.extend(_render_markdown_table(columns, normalized_rows))
        lines.append("")

        notes = str(table.get("notes") or "").strip()
        if notes:
            lines.append(f"**Notes:** {notes}")
            lines.append("")

    if drop_empty_rows or dedupe_rows or dedupe_by_columns:
        lines.append("---")
        lines.append("")
        lines.append("### Conversion Summary")
        lines.append("")
        lines.append(f"- Dropped empty rows: `{total_dropped_empty}`")
        lines.append(f"- Dropped duplicate rows: `{total_dropped_duplicate}`")
        lines.append(f"- Dropped key-duplicate rows: `{total_dropped_key_duplicate}`")
        if dedupe_by_columns:
            lines.append(f"- Dedupe key columns: `{', '.join(dedupe_by_columns)}`")
        lines.append("")

    return "\n".join(lines) + "\n"


def _default_output_path(input_path: Path) -> Path:
    return input_path.with_suffix(".md")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert CTD tabulated JSON to Markdown tables."
    )
    parser.add_argument("input_json", help="Path to the tabulated JSON file.")
    parser.add_argument(
        "-o",
        "--output",
        help="Output Markdown file path (default: same path with .md extension).",
    )
    parser.add_argument(
        "--drop-empty-rows",
        action="store_true",
        help="Drop rows where all columns are empty.",
    )
    parser.add_argument(
        "--dedupe-rows",
        action="store_true",
        help="Drop exact duplicate rows within each table.",
    )
    parser.add_argument(
        "--dedupe-by-column",
        action="append",
        default=[],
        help="Drop duplicate rows by one or more column names (repeatable), e.g. --dedupe-by-column 'Study Number'.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_json).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else _default_output_path(input_path)
    )

    payload = _load_json_with_repair(input_path)
    markdown = _render_md(
        payload=payload,
        source_path=input_path,
        drop_empty_rows=args.drop_empty_rows,
        dedupe_rows=args.dedupe_rows,
        dedupe_by_columns=args.dedupe_by_column,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    print(str(output_path))


if __name__ == "__main__":
    main()
