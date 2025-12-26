# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from __future__ import annotations

from ncd.ingestion.section_detector import extract_section_spans


def test_extract_section_spans_detects_sections_and_pages() -> None:
    markdown = (
        "# Doc\n\n"
        "## Page 1 <ref1>\n\n"
        "Table of Contents\n"
        "1.2 Overview .......... 3\n"
        "1.2 Overview\n"
        "Some text.\n\n"
        "2.1.4 Methods\n"
        "More text.\n\n"
        "## Page 2 <ref2>\n\n"
        "3.1.2.4 Results\n"
        "More.\n"
    )
    sections = extract_section_spans(markdown)
    numbers = [section.section_number for section in sections]

    assert numbers == ["1.2", "2.1.4", "3.1.2.4"]
    assert sections[0].page_start == 1
    assert sections[0].page_end == 1
    assert sections[2].page_start == 2
