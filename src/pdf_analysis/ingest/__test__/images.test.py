# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for pdf_analysis.ingest.images helpers."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, List

import pytest
from PIL import Image

from pdf_analysis.ingest import images


def _png_bytes(color: str = "red") -> bytes:
    buf = BytesIO()
    img = Image.new("RGB", (2, 2), color=color)
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_clamp_bounds() -> None:
    assert images._clamp(5, 1, 10) == 5
    assert images._clamp(-1, 0, 10) == 0
    assert images._clamp(20, 0, 10) == 10


def test_convert_to_pil_from_bytes() -> None:
    img = images._convert_to_pil(_png_bytes())
    assert isinstance(img, Image.Image)


def test_convert_to_pil_from_image() -> None:
    original = Image.new("RGB", (1, 1))
    img = images._convert_to_pil(original)
    assert isinstance(img, Image.Image)


def test_convert_to_pil_from_original_attribute() -> None:
    class Wrapper:
        def __init__(self, data: bytes) -> None:
            self.original = data

    img = images._convert_to_pil(Wrapper(_png_bytes("blue")))
    assert isinstance(img, Image.Image)


def test_convert_to_pil_invalid_type() -> None:
    with pytest.raises(TypeError):
        images._convert_to_pil(object())


def test_extract_image_caption() -> None:
    class FakePage:
        def extract_words(self) -> List[Dict[str, Any]]:
            return [
                {"text": "Caption", "top": 12, "x0": 10},
                {"text": "Here", "top": 12, "x0": 40},
                {"text": "Ignore", "top": 5, "x0": 0},
            ]

    caption = images._extract_image_caption(FakePage(), (0, 0, 100, 10))
    assert caption == "Caption Here"
