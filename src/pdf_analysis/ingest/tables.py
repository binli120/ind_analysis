# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

# @author: Bin Lee
# @email: blee@filynai.com

"""Robust table extraction helpers that wrap pdfplumber, camelot, and tabula."""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import logging

import pandas as pd
import re

logger = logging.getLogger(__name__)

_PDFPLUMBER_COMPATIBLE = True
_HEADER_KEYWORDS = {
    "group",
    "study",
    "species",
    "strain",
    "sex",
    "gender",
    "dose",
    "doses",
    "treatment",
    "route",
    "administration",
    "test",
    "system",
    "method",
    "facility",
    "organ",
    "finding",
    "result",
    "parameter",
    "time",
    "day",
    "week",
    "month",
}
_TITLE_ROW_RE = re.compile(r"\b(table|figure)\b", re.IGNORECASE)
_UNIT_TOKEN_RE = re.compile(r"(?i)\b(mg|kg|g|ug|ml|l|hr|h|min|sec|day|week|month|%)\b")
_META_HEADER_RE = re.compile(
    r"\b(title|page|report|protocol|sponsor|study\s+no|study\s+number|document|confidential|copyright|"
    r"controlled\s+form|study\s+director|version|prepared\s+by|approved\s+by|gxp|gmp|gcp|glp)\b",
    re.IGNORECASE,
)
_LEADER_RE = re.compile(r"^[._\- ]{5,}$")


def _clean_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    return re.sub(r"\s+", " ", text).strip()


def _cell_is_numeric(value: str) -> bool:
    if not value:
        return False
    return bool(re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?%?", value))


def _row_is_mostly_numeric(cells: List[str]) -> bool:
    non_empty = [cell for cell in cells if cell]
    if not non_empty:
        return False
    numeric_cells = sum(1 for cell in non_empty if _cell_is_numeric(cell))
    return numeric_cells / max(len(non_empty), 1) >= 0.6


def _non_empty_ratio(cells: List[str]) -> float:
    if not cells:
        return 0.0
    non_empty = [cell for cell in cells if cell]
    return len(non_empty) / max(len(cells), 1)


def _row_alpha_count(cells: List[str]) -> int:
    return sum(1 for cell in cells if cell and re.search(r"[A-Za-z]", cell))


def _row_has_keywords(cells: List[str]) -> bool:
    for cell in cells:
        if not cell:
            continue
        lowered = cell.lower()
        if any(kw in lowered for kw in _HEADER_KEYWORDS):
            return True
    return False


def _row_header_score(cells: List[str]) -> float:
    non_empty = [cell for cell in cells if cell]
    if not non_empty:
        return 0.0
    alpha_cells = sum(1 for cell in non_empty if re.search(r"[A-Za-z]", cell))
    numeric_cells = sum(1 for cell in non_empty if _cell_is_numeric(cell))
    keyword_hits = 0
    for cell in non_empty:
        lowered = cell.lower()
        keyword_hits += sum(1 for kw in _HEADER_KEYWORDS if kw in lowered)
    return (
        alpha_cells
        + (0.5 * len(non_empty))
        + (1.5 * keyword_hits)
        - (0.75 * numeric_cells)
    )


def _is_title_row(cells: List[str]) -> bool:
    non_empty = [cell for cell in cells if cell]
    if len(non_empty) != 1:
        return False
    text = non_empty[0].lower()
    return bool(_TITLE_ROW_RE.search(text))


def _row_numeric_ratio(cells: List[str]) -> float:
    non_empty = [cell for cell in cells if cell]
    if not non_empty:
        return 0.0
    numeric_cells = sum(1 for cell in non_empty if _cell_is_numeric(cell))
    return numeric_cells / max(len(non_empty), 1)


def _row_alpha_ratio(cells: List[str]) -> float:
    non_empty = [cell for cell in cells if cell]
    if not non_empty:
        return 0.0
    alpha_cells = sum(1 for cell in non_empty if re.search(r"[A-Za-z]", cell))
    return alpha_cells / max(len(non_empty), 1)


def _row_is_metadata_like(cells: List[str]) -> bool:
    non_empty = [cell for cell in cells if cell]
    if not non_empty:
        return False
    text = " ".join(non_empty).lower()
    if not _META_HEADER_RE.search(text):
        return False
    if any(":" in cell for cell in non_empty):
        return True
    if len(non_empty) <= 3:
        return True
    if _looks_like_fragmented_text([non_empty]):
        return True
    if _row_alpha_ratio(non_empty) >= 0.8 and _row_numeric_ratio(non_empty) == 0:
        return True
    return False


def _is_unit_row(cells: List[str]) -> bool:
    non_empty = [cell for cell in cells if cell]
    if not non_empty:
        return False
    keyword_hits = 0
    unit_hits = 0
    for cell in non_empty:
        lowered = cell.lower()
        keyword_hits += sum(1 for kw in _HEADER_KEYWORDS if kw in lowered)
        if _UNIT_TOKEN_RE.search(lowered) or re.fullmatch(r"[()%/.\-\s]+", lowered):
            unit_hits += 1
    if keyword_hits:
        return False
    return unit_hits >= max(1, len(non_empty) // 2)


def _merge_header_rows(primary: List[str], secondary: List[str]) -> List[str]:
    merged: List[str] = []
    max_len = max(len(primary), len(secondary))
    for idx in range(max_len):
        base = primary[idx] if idx < len(primary) else ""
        extra = secondary[idx] if idx < len(secondary) else ""
        if extra and extra not in base:
            merged.append(f"{base} {extra}".strip() if base else extra)
        else:
            merged.append(base)
    return merged


def _apply_label_row(label_row: List[str], header_row: List[str]) -> List[str]:
    merged: List[str] = []
    max_len = max(len(label_row), len(header_row))
    current_label = ""
    for idx in range(max_len):
        label = label_row[idx] if idx < len(label_row) else ""
        if label:
            current_label = label
        cell = header_row[idx] if idx < len(header_row) else ""
        if cell and _cell_is_numeric(cell) and current_label:
            merged.append(f"{current_label} {cell}".strip())
        elif cell:
            merged.append(cell)
        elif label:
            merged.append(label)
        else:
            merged.append("")
    return merged


def _is_metadata_table(header: List[str], data_rows: List[List[str]]) -> bool:
    if _row_is_metadata_like(header):
        return True
    header_text = " ".join(header).lower()
    if not _META_HEADER_RE.search(header_text):
        return False
    if not data_rows:
        return True
    non_empty_rows = [row for row in data_rows if any(cell for cell in row)]
    if not non_empty_rows:
        return True
    if not _row_has_keywords(header):
        return True
    mostly_empty = sum(1 for row in non_empty_rows if _non_empty_ratio(row) <= 0.3)
    if mostly_empty / max(len(non_empty_rows), 1) >= 0.7:
        return True
    meta_rows = 0
    for row in non_empty_rows[:3]:
        row_text = " ".join(row).lower()
        if _META_HEADER_RE.search(row_text):
            meta_rows += 1
    if meta_rows >= max(1, len(non_empty_rows[:3])):
        return True
    return False


def _looks_like_text_block(rows: List[List[str]]) -> bool:
    if not rows:
        return False
    if any(_row_has_keywords(row) and not _row_is_metadata_like(row) for row in rows):
        return False
    candidates = 0
    for row in rows:
        cells = [cell for cell in row if cell]
        if not cells:
            continue
        if any(_cell_is_numeric(cell) for cell in cells):
            continue
        joined = " ".join(cells)
        if len(joined) < 40:
            continue
        avg_len = sum(len(cell) for cell in cells) / max(len(cells), 1)
        if avg_len <= 25:
            candidates += 1
    return candidates >= max(1, len(rows) // 2)


def _looks_like_fragmented_text(rows: List[List[str]]) -> bool:
    if not rows:
        return False
    candidates = 0
    for row in rows:
        cells = [cell for cell in row if cell]
        if len(cells) < 4:
            continue
        if _row_has_keywords(cells):
            continue
        avg_len = sum(len(cell) for cell in cells) / max(len(cells), 1)
        short_cells = sum(1 for cell in cells if len(cell) <= 6)
        if avg_len <= 6 and short_cells / max(len(cells), 1) >= 0.6:
            if _row_alpha_ratio(cells) >= 0.7 and _row_numeric_ratio(cells) == 0:
                candidates += 1
                continue
        if sum(1 for cell in cells if _LEADER_RE.match(cell)) >= max(
            1, len(cells) // 2
        ):
            candidates += 1
            continue
        joined = " ".join(cells)
        if len(joined) < 30:
            continue
        avg_len = sum(len(cell) for cell in cells) / max(len(cells), 1)
        if avg_len <= 12:
            candidates += 1
    return candidates >= max(1, len(rows) // 2)


def _recover_header_and_data(
    table: List[List[str]],
) -> tuple[List[str], List[List[str]]]:
    rows = [[_clean_cell(cell) for cell in row] for row in table if row]
    if not rows:
        return [], []
    if len(rows) == 1:
        return rows[0], []

    candidate_count = min(8, len(rows))
    best_idx = 0
    best_score = -1.0
    for idx in range(candidate_count):
        cells = rows[idx]
        if _is_title_row(cells):
            continue
        if _row_is_metadata_like(cells):
            continue
        if _row_is_mostly_numeric(cells):
            continue
        meta_hit = _META_HEADER_RE.search(" ".join(cells).lower())
        score = _row_header_score(cells)
        score += 2.5 * _row_alpha_ratio(cells)
        score -= 2.0 * _row_numeric_ratio(cells)
        if meta_hit and not _row_has_keywords(cells):
            score -= 4.0
        if _non_empty_ratio(cells) < 0.3:
            score -= 1.5
        if score > best_score and not _row_is_mostly_numeric(cells):
            best_score = score
            best_idx = idx

    header_idx = best_idx
    header = rows[header_idx]

    if _row_is_metadata_like(header):
        for offset in range(1, min(3, len(rows) - header_idx)):
            candidate_idx = header_idx + offset
            candidate = rows[candidate_idx]
            if _row_is_metadata_like(candidate):
                continue
            if _row_is_mostly_numeric(candidate):
                continue
            if (
                _row_has_keywords(candidate)
                or _row_header_score(candidate) >= _row_header_score(header) + 1
            ):
                header_idx = candidate_idx
                header = candidate
                break

    if header_idx > 0:
        prev = rows[header_idx - 1]
        if (
            _row_is_mostly_numeric(header)
            and not _row_is_mostly_numeric(prev)
            and _row_alpha_count(prev) >= 1
            and _row_has_keywords(prev)
            and not _row_is_metadata_like(prev)
        ):
            header = _apply_label_row(prev, header)
        elif (
            not _is_title_row(prev)
            and _row_header_score(prev) >= 1.5
            and not _row_is_mostly_numeric(prev)
            and not _row_is_metadata_like(prev)
        ):
            header = _merge_header_rows(prev, header)

    data_start = header_idx + 1
    if data_start < len(rows):
        next_row = rows[data_start]
        if (
            _row_is_mostly_numeric(next_row)
            and _row_has_keywords(header)
            and _non_empty_ratio(header) <= 0.6
        ):
            header = _apply_label_row(header, next_row)
            data_start += 1
        elif _non_empty_ratio(header) <= 0.5 and _row_alpha_count(next_row) >= 2:
            if _row_has_keywords(next_row) or _row_header_score(next_row) >= 1.5:
                header = _apply_label_row(header, next_row)
                data_start += 1
        elif _row_alpha_count(header) < 2 and _row_alpha_count(next_row) >= 2:
            if not _row_is_mostly_numeric(next_row) and _row_header_score(
                next_row
            ) > _row_header_score(header):
                header = next_row
                data_start += 1
    if data_start < len(rows) and _is_unit_row(rows[data_start]):
        header = _merge_header_rows(header, rows[data_start])
        data_start += 1

    data_rows = rows[data_start:]
    return header, data_rows


def _plumber_tables_on_page(page) -> List[pd.DataFrame]:
    """Try multiple pdfplumber strategies to maximize table recall."""
    dfs: List[pd.DataFrame] = []

    # Strategy A: 'lines' + explicit vertical/horizontal tolerance
    tables = _safe_extract_tables(
        page,
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 5,
        },
    )
    dfs += _tables_to_dfs(tables)

    # Strategy B: 'text' stream (good for non-ruled tables)
    tables = _safe_extract_tables(
        page,
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "snap_tolerance": 3,
            "join_tolerance": 3,
            "edge_min_length": 3,
        },
    )
    dfs += _tables_to_dfs(tables)

    # Strategy C: default
    tables = _safe_extract_tables(page, None)
    dfs += _tables_to_dfs(tables)

    # De-dup roughly by shape + first row hash
    uniq = []
    seen = set()
    for df in dfs:
        key = (df.shape, tuple(df.iloc[0].astype(str)) if not df.empty else ("",))
        if key not in seen:
            seen.add(key)
            uniq.append(df)
    return uniq


def _tables_to_dfs(tables: List[List[List[str]]]) -> List[pd.DataFrame]:
    out = []
    for tbl in tables or []:
        df = _build_dataframe_from_rows(tbl)
        if df is not None:
            out.append(df)
    return out


def _build_dataframe_from_rows(table: List[List[str]]) -> Optional[pd.DataFrame]:
    if not table or len(table) < 2:
        return None
    cleaned = [[_clean_cell(cell) for cell in row] for row in table if row]
    if _looks_like_text_block(cleaned) or _looks_like_fragmented_text(cleaned):
        return None
    header, data = _recover_header_and_data(cleaned)
    if not data:
        return None
    if _row_is_metadata_like(header):
        return None
    if _is_metadata_table(header, data):
        return None
    max_len = max(len(header), max(len(row) for row in data if row))
    header = header + [""] * (max_len - len(header))
    columns = [(h or "").strip() or f"col_{i + 1}" for i, h in enumerate(header)]
    normalized_rows = [row + [""] * (max_len - len(row)) for row in data]
    return pd.DataFrame(normalized_rows, columns=columns)


def _dataframe_to_rows(df: pd.DataFrame) -> List[List[str]]:
    table_rows = df.fillna("").astype(str).values.tolist()
    return [[_clean_cell(cell) for cell in row] for row in table_rows]


def _safe_extract_tables(page, settings):
    global _PDFPLUMBER_COMPATIBLE
    if not _PDFPLUMBER_COMPATIBLE:
        return []
    try:
        if settings is None:
            return page.extract_tables()
        return page.extract_tables(settings)
    except AttributeError as exc:
        if "graphicstate" in str(exc) or "original_path" in str(exc):
            logger.warning(
                "pdfplumber table extraction disabled due to compatibility issue: %s",
                exc,
            )
            _PDFPLUMBER_COMPATIBLE = False
        else:
            logger.warning(
                "pdfplumber failed to parse tables on page %s: %s",
                page.page_number,
                exc,
            )
    except Exception as exc:  # pragma: no cover - defensive bailout
        logger.warning(
            "pdfplumber table extraction error on page %s: %s",
            page.page_number,
            exc,
        )
    return []


def _normalise_page_selection(
    pages: Optional[Sequence[int]], max_pages: Optional[int]
) -> Optional[List[int]]:
    """
    Normalises requested pages (1-based) to a sorted, unique list.
    When both pages and max_pages are provided the intersection is returned.
    """
    if pages is not None:
        selected = sorted({p for p in pages if p > 0})
        if max_pages is not None:
            return [p for p in selected if p <= max_pages]
        return selected
    if max_pages is not None:
        return list(range(1, max_pages + 1))
    return None


def extract_tables(
    pdf_path: Path,
    engine: str = "pdfplumber",
    *,
    pages: Optional[Sequence[int]] = None,
    max_pages: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Returns list of:
      {
        "page_number": int,
        "index_on_page": int,
        "engine": str,
        "dataframe": pd.DataFrame
      }
    """
    results: List[Dict[str, Any]] = []
    page_selection = _normalise_page_selection(pages, max_pages)

    if engine == "pdfplumber":
        try:
            from pdfminer import pdfinterp as _pdfinterp  # type: ignore

            if not hasattr(_pdfinterp, "PDFStackT"):
                from typing import Any as _Any

                _pdfinterp.PDFStackT = _Any  # type: ignore[attr-defined]
        except Exception:
            pass

        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            total_pages = len(pdf.pages)
            indices: Iterable[int]
            if page_selection is None:
                indices = range(total_pages)
            else:
                indices = (p - 1 for p in page_selection if 0 < p <= total_pages)

            for i in indices:
                page = pdf.pages[i]
                dfs = _plumber_tables_on_page(page)
                for j, df in enumerate(dfs, start=1):
                    results.append(
                        {
                            "page_number": i + 1,
                            "index_on_page": j,
                            "engine": "pdfplumber",
                            "dataframe": df,
                        }
                    )

    elif engine == "camelot":
        try:
            import camelot
        except Exception as e:
            raise RuntimeError(
                "camelot-py not installed. `poetry install --with camelot`"
            ) from e
        if page_selection is None:
            page_range = f"1-{max_pages}" if max_pages else "all"
        else:
            page_range = ",".join(str(p) for p in page_selection)

        # Lattice for ruled tables, Stream for non-ruled; try both
        for flavor in ("lattice", "stream"):
            try:
                tables = camelot.read_pdf(
                    str(pdf_path), flavor=flavor, pages=page_range
                )
                for t in tables:
                    df = t.df
                    pno = t.page
                    results.append(
                        {
                            "page_number": pno,
                            "index_on_page": 1,
                            "engine": f"camelot/{flavor}",
                            "dataframe": df,
                        }
                    )
            except Exception:
                pass  # continue

    elif engine == "tabula":
        try:
            import tabula
        except Exception as e:
            raise RuntimeError(
                "tabula-py not installed. `poetry install --with tabula`"
            ) from e

        if page_selection is None:
            page_range = f"1-{max_pages}" if max_pages else "all"
        else:
            page_range = ",".join(str(p) for p in page_selection)

        try:
            dfs = tabula.read_pdf(str(pdf_path), pages=page_range, multiple_tables=True)
            for idx, df in enumerate(dfs, start=1):
                normalized = _build_dataframe_from_rows(_dataframe_to_rows(df))
                if normalized is None:
                    continue
                results.append(
                    {
                        "page_number": (
                            page_selection[idx - 1]
                            if page_selection and idx <= len(page_selection)
                            else idx
                        ),
                        "index_on_page": 1,
                        "engine": "tabula",
                        "dataframe": normalized,
                    }
                )
        except Exception:
            pass

    return results


def extract_tables_all(
    pdf_path: Path, engine: str = "pdfplumber", max_pages: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Backwards-compatible wrapper that extracts tables across the document.
    """
    return extract_tables(pdf_path, engine=engine, max_pages=max_pages)
