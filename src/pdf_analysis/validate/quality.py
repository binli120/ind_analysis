"""Quality scoring and issue detection for extracted documents."""

# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass
class QualityIssue:
    severity: str  # "error", "warning", "info"
    message: str
    context: str
    suggestion: Optional[str] = None


def _normalize_identifier(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _extract_int(value: str) -> Optional[int]:
    match = re.search(r"\d+", value)
    if match:
        try:
            return int(match.group())
        except ValueError:
            return None
    return None


def _parse_date(value: str) -> Optional[datetime]:
    cleaned = value.strip()
    if not cleaned:
        return None
    candidates = [
        "%m-%Y",
        "%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y",
    ]
    for pattern in candidates:
        try:
            return datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
    return None


def _is_generic_header(name: Any) -> bool:
    if name is None:
        return True
    name_str = str(name).strip().lower()
    if not name_str:
        return True
    if name_str.startswith("col_"):
        return True
    if re.fullmatch(r"\d+", name_str):
        return True
    return False


def _extract_key_values(df: pd.DataFrame) -> Dict[str, str]:
    if df.shape[1] != 2:
        return {}
    left = df.iloc[:, 0].astype(str).str.strip()
    right = df.iloc[:, 1].astype(str).str.strip()
    if left.empty:
        return {}
    unique_ratio = left.nunique() / max(len(left), 1)
    if unique_ratio < 0.7:
        return {}
    result: Dict[str, str] = {}
    for left_val, right_val in zip(left, right):
        segments = [seg.strip() for seg in re.split(r"[\r\n]+", left_val)]
        matched = False
        last_key: Optional[str] = None
        for segment in segments:
            if not segment:
                continue
            if ":" in segment:
                key_part, value_part = segment.split(":", 1)
                key = key_part.strip()
                value = value_part.strip()
                if key:
                    result[key] = value
                    matched = True
                    last_key = key
            elif matched and last_key:
                result[last_key] = (result[last_key] + " " + segment).strip()
        if matched:
            if right_val and right_val.lower().startswith("page"):
                result.setdefault("Page Info", right_val)
            continue
        if left_val:
            result[left_val] = right_val
    return result


def _missing_cell_count(df: pd.DataFrame) -> int:
    is_na = df.isna()
    as_str = df.astype(str)
    is_blank = as_str.apply(lambda col: col.str.strip() == "")
    mask = is_na | is_blank
    return int(mask.to_numpy().sum())


def generate_quality_report(
    pdf_path: Path,
    pages: List[Dict[str, Any]],
    tables: List[Dict[str, Any]],
    *,
    extraction_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build quality report (json + markdown) for extracted document.
    """
    issues: List[QualityIssue] = []
    kv_pairs: Dict[str, str] = {}
    kv_sources: Dict[str, str] = {}

    for table in tables:
        df: pd.DataFrame = table["dataframe"]
        table_id = f"Page {table['page_number']} table {table['index_on_page']}"
        if df.empty:
            issues.append(
                QualityIssue(
                    severity="error",
                    message="Table has no rows after extraction.",
                    context=table_id,
                    suggestion="Verify the table detection parameters or consider OCR fallback.",
                )
            )
            continue

        if any(_is_generic_header(col) for col in df.columns):
            issues.append(
                QualityIssue(
                    severity="warning",
                    message="Table headers appear generic or auto-generated.",
                    context=table_id,
                    suggestion="Inspect the source table to ensure headers were captured correctly.",
                )
            )

        missing_cells = _missing_cell_count(df)
        if missing_cells:
            issues.append(
                QualityIssue(
                    severity="warning",
                    message=f"Detected {missing_cells} blank cell(s) in extracted table.",
                    context=table_id,
                    suggestion="Check for split columns or OCR errors in the source PDF.",
                )
            )

        kv = _extract_key_values(df)
        for key, value in kv.items():
            kv_pairs[key] = value
            kv_sources[key] = table_id

    # Cross-check metadata if present
    doc_stem = pdf_path.stem
    if kv_pairs:
        report_number = kv_pairs.get("Report Number") or kv_pairs.get("Report Number:")
        if report_number:
            extracted_id = _normalize_identifier(report_number)
            stem_id = _normalize_identifier(doc_stem)
            if extracted_id and stem_id and extracted_id != stem_id:
                issues.append(
                    QualityIssue(
                        severity="warning",
                        message=f"Report number '{report_number}' does not match file name '{doc_stem}'.",
                        context=kv_sources.get("Report Number", "metadata"),
                        suggestion="Ensure the correct PDF is associated with this metadata.",
                    )
                )

        declared_pages = None
        if "Number of Pages" in kv_pairs:
            declared_pages = _extract_int(kv_pairs["Number of Pages"])
        elif "Pages" in kv_pairs:
            declared_pages = _extract_int(kv_pairs["Pages"])

        total_pages_extracted = len(pages)
        if declared_pages is not None:
            if extraction_limit is not None and total_pages_extracted >= extraction_limit:
                issues.append(
                    QualityIssue(
                        severity="info",
                        message=(
                            "Page count validation skipped because extraction was limited "
                            f"to {extraction_limit} page(s). Declared total: {declared_pages}."
                        ),
                        context="metadata",
                    )
                )
            elif declared_pages != total_pages_extracted:
                issues.append(
                    QualityIssue(
                        severity="warning",
                        message=(
                            f"Declared number of pages ({declared_pages}) does not match "
                            f"extracted page count ({total_pages_extracted})."
                        ),
                        context=kv_sources.get("Number of Pages", "metadata"),
                        suggestion="Confirm the PDF is complete and re-run extraction without page limits.",
                    )
                )

        # Date fields validation
        date_fields: Dict[str, datetime] = {}
        for key, value in kv_pairs.items():
            if "date" in key.lower():
                parsed = _parse_date(value)
                if parsed is None and value.strip():
                    issues.append(
                        QualityIssue(
                            severity="warning",
                            message=f"Date value '{value}' could not be parsed.",
                            context=kv_sources.get(key, "metadata"),
                            suggestion="Ensure the date follows a standard format (e.g., MM-YYYY or MM/DD/YYYY).",
                        )
                    )
                elif parsed is not None:
                    date_fields[key] = parsed

        if (
            "Initiation Date" in date_fields
            and "Completion Date" in date_fields
            and date_fields["Completion Date"] < date_fields["Initiation Date"]
        ):
            issues.append(
                QualityIssue(
                    severity="error",
                    message="Completion Date occurs before Initiation Date.",
                    context=kv_sources.get("Completion Date", "metadata"),
                    suggestion="Verify the initiation and completion dates in the source document.",
                )
            )

    # Build outputs
    stats = {
        "document": pdf_path.name,
        "pages_extracted": len(pages),
        "tables_extracted": len(tables),
    }

    json_report = {
        "document": pdf_path.name,
        "stats": stats,
        "issues": [asdict(issue) for issue in issues],
        "metadata": kv_pairs,
    }

    if extraction_limit is not None:
        json_report["extraction_limit"] = str(extraction_limit)

    md_lines = [
        f"# Data Quality Report — {pdf_path.name}",
        "",
        f"- Pages extracted: {stats['pages_extracted']}",
        f"- Tables extracted: {stats['tables_extracted']}",
    ]
    if extraction_limit is not None:
        md_lines.append(f"- Extraction limit: {extraction_limit} page(s)")
    md_lines.append("")
    md_lines.append("## Issues")
    if not issues:
        md_lines.append("No data quality issues detected.")
    else:
        for issue in issues:
            line = f"- **{issue.severity.upper()}** ({issue.context}) — {issue.message}"
            if issue.suggestion:
                line += f" _Suggestion_: {issue.suggestion}"
            md_lines.append(line)

    if kv_pairs:
        md_lines.append("")
        md_lines.append("## Extracted Metadata")
        for key, value in kv_pairs.items():
            md_lines.append(f"- **{key}**: {value}")

    markdown_report = "\n".join(md_lines)

    return {"json": json_report, "markdown": markdown_report}
