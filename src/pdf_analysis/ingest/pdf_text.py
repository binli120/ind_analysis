# @author: Bin Lee
# @email: blee@filynai.com

from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage

from .ocr import ocr_pages_if_needed


def _extract_page_text(page, rsrcmgr: PDFResourceManager, laparams: LAParams) -> str:
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


def extract_pages_text(
    pdf_path: Path,
    ocr_fallback: bool = False,
    max_pages: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Returns list of pages with:
      { "page_number": int, "text": str }
    If a page yields no text and ocr_fallback=True, runs OCR on that page image.
    """
    pages_out: List[Dict[str, Any]] = []
    empty_page_ids: List[int] = []

    laparams = LAParams()
    with pdf_path.open("rb") as fh:
        for idx, page in enumerate(PDFPage.get_pages(fh), start=1):
            if max_pages is not None and idx > max_pages:
                break
            rsrcmgr = PDFResourceManager()
            text = _extract_page_text(page, rsrcmgr, laparams).strip()
            pages_out.append({"page_number": idx, "text": text})
            if not text:
                empty_page_ids.append(idx - 1)

    if ocr_fallback and empty_page_ids:
        ocr_texts = ocr_pages_if_needed(pdf_path, empty_page_ids)
        for idx, txt in ocr_texts.items():
            pages_out[idx]["text"] = txt

    return pages_out
