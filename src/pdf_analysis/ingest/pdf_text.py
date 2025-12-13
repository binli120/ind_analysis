"""PDF text extraction utilities with optional OCR fallback."""

# @author: Bin Lee
# @email: blee@filynai.com

from io import StringIO
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage

from .ocr import ocr_pages_if_needed


def _extract_page_text(
    page: PDFPage, rsrcmgr: PDFResourceManager, laparams: LAParams
) -> str:
    """
    Render a single PDF page to text using pdfminer primitives.
    """
    output = StringIO()
    device = TextConverter(rsrcmgr, output, laparams=laparams)
    interpreter = PDFPageInterpreter(rsrcmgr, device)
    interpreter.process_page(page)
    device.close()
    text = output.getvalue()
    output.close()
    return text


def iter_pages_text(
    pdf_path: Path,
    *,
    page_numbers: Optional[Sequence[int]] = None,
    max_pages: Optional[int] = None,
) -> Iterator[Dict[str, str]]:
    """
    Yields page dictionaries with text content in the original document order.
    The caller is responsible for any OCR fallback or post-processing.
    """
    laparams = LAParams()
    selection: Optional[set[int]] = None
    if page_numbers is not None:
        # Accept 1-based page numbers to match user-facing conventions.
        selection = {p for p in page_numbers if p > 0}

    with pdf_path.open("rb") as fh:
        for idx, page in enumerate(PDFPage.get_pages(fh), start=1):
            if max_pages is not None and idx > max_pages:
                break
            if selection is not None and idx not in selection:
                continue

            rsrcmgr = PDFResourceManager()
            text = _extract_page_text(page, rsrcmgr, laparams).strip()
            yield {"page_number": str(idx), "text": text}


def extract_pages_text(
    pdf_path: Path,
    ocr_fallback: bool = False,
    max_pages: Optional[int] = None,
) -> List[Dict[str, str]]:
    """
    Returns list of pages with:
      { "page_number": int, "text": str }
    If a page yields no text and ocr_fallback=True, runs OCR on that page image.
    """
    pages_out: List[Dict[str, str]] = list(
        iter_pages_text(pdf_path, max_pages=max_pages)
    )
    empty_page_ids: List[int] = [
        int(entry["page_number"]) - 1
        for entry in pages_out
        if entry.get("page_number") is not None and not entry.get("text")
    ]

    if ocr_fallback and empty_page_ids:
        ocr_texts = ocr_pages_if_needed(pdf_path, empty_page_ids)
        for idx, txt in ocr_texts.items():
            pages_out[idx]["text"] = txt

    return pages_out
