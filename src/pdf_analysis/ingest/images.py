# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Image extraction helpers for PDF processing."""

# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pdfplumber
from PIL import Image


def _clamp(val: float, minv: float, maxv: float) -> float:
    return max(minv, min(val, maxv))


def _convert_to_pil(raw_image: Any) -> Image.Image:
    if isinstance(raw_image, (bytes, bytearray)):
        return Image.open(io.BytesIO(raw_image))
    if isinstance(raw_image, Image.Image):
        return raw_image
    if hasattr(raw_image, "original"):
        orig = raw_image.original
        if isinstance(orig, (bytes, bytearray)):
            return Image.open(io.BytesIO(orig))
        if isinstance(orig, Image.Image):
            return orig
    raise TypeError("Unknown image format from pdfplumber.to_image()")


def _extract_image_caption(page: Any, bbox: tuple[float, float, float, float], window: int = 60) -> str | None:
    x0, top, x1, bottom = bbox
    words = page.extract_words() or []
    caption_words = [w for w in words if bottom <= w.get("top", 0) <= bottom + window]
    caption_words.sort(key=lambda w: (w.get("top", 0), w.get("x0", 0)))
    caption = " ".join(w.get("text", "") for w in caption_words).strip()
    return caption or None


def extract_images(
    pdf_path: Path | str,
    image_dir: Path | str,
    *,
    page_limit: Optional[int] = None,
    resolution: int = 200,
) -> List[Dict[str, Any]]:
    """Extract images from PDF pages, save PNGs, and return metadata."""
    out_dir = Path(image_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    image_refs: List[Dict[str, Any]] = []

    with pdfplumber.open(str(pdf_path)) as pdfdoc:
        for page_idx, page in enumerate(pdfdoc.pages, start=1):
            if page_limit and page_idx > page_limit:
                break
            for img_idx, img in enumerate(page.images, start=1):
                page_x0, page_y0, page_x1, page_y1 = page.bbox
                x0 = _clamp(img.get("x0", page_x0), page_x0, page_x1)
                x1 = _clamp(img.get("x1", page_x0), page_x0, page_x1)
                top = _clamp(img.get("top", page_y0), page_y0, page_y1)
                bottom = _clamp(img.get("bottom", page_y1), page_y0, page_y1)
                if x1 <= x0 or bottom <= top:
                    continue

                crop = page.crop((x0, top, x1, bottom))
                raw = crop.to_image(resolution=resolution)
                pil = _convert_to_pil(raw)

                img_name = f"image_page_{page_idx}_idx_{img_idx}.png"
                img_path = out_dir / img_name
                pil.save(img_path)

                caption = _extract_image_caption(page, (x0, top, x1, bottom))
                if not caption:
                    caption = f"Image page {page_idx} #{img_idx}"

                image_refs.append(
                    {
                        "page_number": page_idx,
                        "index_on_page": img_idx,
                        "file_path": str(img_path),
                        "caption": caption,
                        "bbox": [x0, top, x1, bottom],
                    }
                )

    return image_refs
