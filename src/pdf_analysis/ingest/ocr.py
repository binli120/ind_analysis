from pathlib import Path
from typing import Dict, List

try:
    import pytesseract
    from pdf2image import convert_from_path
except Exception:
    pytesseract = None
    convert_from_path = None


def ocr_pages_if_needed(pdf_path: Path, zero_based_page_ids: List[int]) -> Dict[int, str]:
    """
    Returns dict {page_index: ocr_text}. Requires pytesseract + pdf2image.
    """
    if pytesseract is None or convert_from_path is None:
        return {}

    # Convert only needed pages to images
    pages = convert_from_path(str(pdf_path), dpi=300, first_page=min(zero_based_page_ids) + 1,
                              last_page=max(zero_based_page_ids) + 1)
    # The returned list is contiguous from first_page..last_page
    base = min(zero_based_page_ids)
    out = {}
    for k, img in enumerate(pages):
        page_idx = base + k
        if page_idx in zero_based_page_ids:
            text = pytesseract.image_to_string(img) or ""
            out[page_idx] = text.strip()
    return out
