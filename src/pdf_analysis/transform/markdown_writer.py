# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Utilities for rendering markdown and HTML representations of extraction results."""

# @author: Bin Lee
# @email: blee@longooc.com

from html import escape
from typing import Any, Dict, List

import pandas as pd

PAGE_BREAK = "\n\n---\n\n"


def _coerce_preview_dataframe(preview: Any) -> pd.DataFrame:
    """Coerce table preview content into a DataFrame."""
    if isinstance(preview, pd.DataFrame):
        return preview
    if isinstance(preview, list):
        rows = [row for row in preview if isinstance(row, dict)]
        return pd.DataFrame(rows)
    if isinstance(preview, dict):
        return pd.DataFrame([preview])
    return pd.DataFrame()


def _df_to_markdown_table(df: Any) -> str:
    """
    Render a pandas DataFrame as a GitHub-flavored markdown table.
    """
    df = _coerce_preview_dataframe(df)
    if df.empty and len(df.columns) == 0:
        return "_(no table preview rows)_"
    md = []
    cols = [str(c) if c is not None else "" for c in df.columns]
    md.append("| " + " | ".join(cols) + " |")
    md.append("| " + " | ".join(["---"] * len(cols)) + " |")
    for _, row in df.iterrows():
        md.append(
            "| " + " | ".join("" if v is None else str(v) for v in row.tolist()) + " |"
        )
    return "\n".join(md)


def build_markdown_document(
    doc_name: str,
    pages: List[Dict[str, Any]],
    table_manifest: List[Dict[str, Any]],
) -> str:
    """
    Build the editable markdown document combining page text and table previews.
    """
    page_header = "> \u00a9 longooc.com | Author: Bin Lee | Email: blee@longooc.com"
    tables_by_page: Dict[int, List[Dict[str, Any]]] = {}
    for table in table_manifest:
        tables_by_page.setdefault(table["page_number"], []).append(table)

    parts = [
        f"# {doc_name}",
        "",
        "> Editable extraction draft including text and tables.",
        "",
    ]
    for page in pages:
        page_number = page["page_number"]
        text = page.get("text", "") or ""
        parts.append(f"## Page {page_number} <ref{page_number}>\n")
        parts.append(page_header)
        parts.append(text if text.strip() else "_(no extractable text)_")

        for table in tables_by_page.get(page_number, []):
            parts.append("")
            header = f"**Table (p{page_number} t{table['index_on_page']})**"
            links = []
            csv_path = table.get("csv")
            json_path = table.get("json")
            if csv_path:
                links.append(f"[CSV]({csv_path})")
            if json_path:
                links.append(f"[JSON]({json_path})")
            if links:
                header += "  \n" + " · ".join(links)
            parts.append(header)
            preview = table.get("preview_rows")
            if preview is not None:
                parts.append("")
                parts.append(_df_to_markdown_table(preview))
        parts.append(PAGE_BREAK)

    return "\n".join(parts)


def _df_to_html_table(df: Any) -> str:
    """
    Render DataFrame preview into HTML table. We rely on pandas' to_html to escape.
    """
    df = _coerce_preview_dataframe(df)
    if df.empty and len(df.columns) == 0:
        return "<p><em>(no table preview rows)</em></p>"
    return df.to_html(index=False, escape=True, border=0, classes=["table-preview"])


def _text_to_html(text: str) -> str:
    if not text.strip():
        return "<p><em>(no extractable text)</em></p>"
    paragraphs = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = "<br />".join(escape(line) for line in block.splitlines())
        paragraphs.append(f"<p>{lines}</p>")
    return (
        "\n".join(paragraphs) if paragraphs else "<p><em>(no extractable text)</em></p>"
    )


def build_html_document(
    doc_name: str,
    pages: List[Dict[str, Any]],
    table_manifest: List[Dict[str, Any]],
) -> str:
    """
    Build an HTML representation ready for rich-text editors.
    """
    tables_by_page: Dict[int, List[Dict[str, Any]]] = {}
    for table in table_manifest:
        tables_by_page.setdefault(table["page_number"], []).append(table)
    page_header_html = '<p class="page-meta">&copy; longooc.com | Author: Bin Lee | Email: blee@longooc.com</p>'

    parts = [
        '<article class="pdf-extraction" data-source="pdf">',
        f"<h1>{escape(doc_name)}</h1>",
        '<p class="lead">Editable extraction draft including text and tables.</p>',
    ]
    for page in pages:
        page_number = page["page_number"]
        parts.append(f'<section data-page="{page_number}">')
        parts.append(
            f'<h2>Page {page_number} <span class="page-ref">ref{page_number}</span></h2>'
        )
        parts.append(page_header_html)
        parts.append(_text_to_html(page.get("text", "") or ""))

        for table in tables_by_page.get(page_number, []):
            parts.append('<div class="table-block">')
            links = []
            csv_path = table.get("csv")
            json_path = table.get("json")
            if csv_path:
                links.append(f'<a href="{escape(csv_path)}">CSV</a>')
            if json_path:
                links.append(f'<a href="{escape(json_path)}">JSON</a>')
            if links:
                link_markup = " · ".join(links)
                parts.append(
                    f"<p><strong>Table (p{page_number} t{table['index_on_page']})</strong> {link_markup}</p>"
                )
            else:
                parts.append(
                    f"<p><strong>Table (p{page_number} t{table['index_on_page']})</strong></p>"
                )
            preview = table.get("preview_rows")
            if preview is not None:
                parts.append(_df_to_html_table(preview))
            parts.append("</div>")
        parts.append("</section>")
    parts.append("</article>")
    return "\n".join(parts)
