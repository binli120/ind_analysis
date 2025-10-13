from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


def _plumber_tables_on_page(page) -> List[pd.DataFrame]:
    """Try multiple pdfplumber strategies to maximize table recall."""
    dfs: List[pd.DataFrame] = []

    # Strategy A: 'lines' + explicit vertical/horizontal tolerance
    tables = page.extract_tables(
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 5,
        }
    )
    dfs += _tables_to_dfs(tables)

    # Strategy B: 'text' stream (good for non-ruled tables)
    tables = page.extract_tables(
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "snap_tolerance": 3,
            "join_tolerance": 3,
            "edge_min_length": 3,
        }
    )
    dfs += _tables_to_dfs(tables)

    # Strategy C: default
    tables = page.extract_tables()
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
        if not tbl or len(tbl) < 2:
            continue
        # Heuristic header row
        header = tbl[0]
        data = tbl[1:]
        df = pd.DataFrame(
            data,
            columns=[(h or "").strip() or f"col_{i + 1}" for i, h in enumerate(header)],
        )
        out.append(df)
    return out


def extract_tables_all(
        pdf_path: Path, engine: str = "pdfplumber", max_pages: Optional[int] = None
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

    page_range = "all"
    if max_pages is not None:
        page_range = f"1-{max_pages}"

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
            num_pages = len(pdf.pages)
            limit = min(num_pages, max_pages) if max_pages else num_pages
            for i in range(limit):
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
        # Lattice for ruled tables, Stream for non-ruled; try both
        for flavor in ("lattice", "stream"):
            try:
                tables = camelot.read_pdf(str(pdf_path), flavor=flavor, pages=page_range)
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
        try:
            dfs = tabula.read_pdf(str(pdf_path), pages=page_range, multiple_tables=True)
            for idx, df in enumerate(dfs, start=1):
                results.append(
                    {
                        "page_number": idx,
                        "index_on_page": 1,
                        "engine": "tabula",
                        "dataframe": df,
                    }
                )
        except Exception:
            pass

    return results
