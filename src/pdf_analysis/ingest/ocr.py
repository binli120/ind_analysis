# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Optional OCR fallbacks leveraging pytesseract/pdf2image."""

# @author: Bin Lee
# @email: blee@longooc.com

import logging
from pathlib import Path
from typing import Dict, List

try:
    import pytesseract
    from pdf2image import convert_from_path
    from pdf2image.exceptions import PDFInfoNotInstalledError
except Exception:
    pytesseract = None
    convert_from_path = None  # type: ignore[assignment]
    PDFInfoNotInstalledError = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def ocr_pages_if_needed(
    pdf_path: Path, zero_based_page_ids: List[int], lang: str = "eng"
) -> Dict[int, str]:
    """
    Returns dict {page_index: ocr_text}. Requires pytesseract + pdf2image.
    """
    if pytesseract is None or convert_from_path is None:
        return {}

    try:
        pages = convert_from_path(
            str(pdf_path),
            dpi=300,
            first_page=min(zero_based_page_ids) + 1,
            last_page=max(zero_based_page_ids) + 1,
        )
    except FileNotFoundError as exc:
        logger.warning("pdf2image Popper binary not found: %s", exc)
        return {}
    except Exception as exc:  # pylint: disable=broad-except
        if PDFInfoNotInstalledError is not None and isinstance(
            exc, PDFInfoNotInstalledError
        ):
            logger.warning(
                "poppler is required for pdf2image OCR fallback. Install poppler and ensure it is on PATH."
            )
        else:
            logger.warning("pdf2image failed to rasterize pages: %s", exc)
        return {}

    # The returned list is contiguous from first_page..last_page
    base = min(zero_based_page_ids)
    out: Dict[int, str] = {}
    for k, img in enumerate(pages):
        page_idx = base + k
        if page_idx in zero_based_page_ids:
            text = pytesseract.image_to_string(img, lang=lang) or ""
            out[page_idx] = text.strip()
    return out
