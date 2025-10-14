# @author: Bin Lee
# @email: blee@filynai.com

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def save_tables(
        pdf_path: Path, tables: List[Dict[str, Any]], outdir: Path
) -> List[Dict[str, Any]]:
    """
    Saves each table as CSV and JSON, returns manifest with preview rows for
    Markdown.
    """
    manifest: List[Dict[str, Any]] = []
    stem = Path(pdf_path).stem
    for t in tables:
        pno = t["page_number"]
        idx = t["index_on_page"]
        df: pd.DataFrame = t["dataframe"]

        # Clean up column names/values
        df = df.fillna("").astype(str)

        csv_path = outdir / f"{stem}.p{pno}.t{idx}.csv"
        json_path = outdir / f"{stem}.p{pno}.t{idx}.json"
        df.to_csv(csv_path, index=False)
        df.to_json(json_path, orient="records", force_ascii=False, indent=2)

        manifest.append(
            {
                "page_number": pno,
                "index_on_page": idx,
                "engine": t["engine"],
                "csv": csv_path.name,
                "json": json_path.name,
                "preview_rows": df.head(10),  # embed top-10 as markdown table
            }
        )
    return manifest
