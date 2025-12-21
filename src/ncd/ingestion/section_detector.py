from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

SECTION_LINE_RE = re.compile(
    r"(?m)^(?P<leading>\s{0,6}(?:Section|SECTION|Sec\.?)?\s*)"
    r"(?P<section>\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9]+)?)"
    r"(?P<tail>[^\n]*)$"
)

PAGE_MARKER_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?m)^##\s+Page\s+(?P<page>\d+)\b"),
    re.compile(r"(?m)^<!--\s*Page\s+(?P<page>\d+)\s*-->"),
)


@dataclass(slots=True)
class SectionMarker:
    section_number: str
    section_title: Optional[str]
    offset: int


@dataclass(slots=True)
class SectionSpan:
    section_number: str
    section_title: Optional[str]
    char_start: int
    char_end: int
    page_start: Optional[int]
    page_end: Optional[int]


def extract_section_spans(markdown: str) -> List[SectionSpan]:
    markers = _find_section_markers(markdown)
    if not markers:
        return []

    page_markers = _find_page_markers(markdown)
    sections: List[SectionSpan] = []

    for idx, marker in enumerate(markers):
        start = marker.offset
        end = markers[idx + 1].offset if idx + 1 < len(markers) else len(markdown)
        page_start = _page_for_offset(page_markers, start)
        page_end = _page_for_offset(page_markers, max(start, end - 1))
        sections.append(
            SectionSpan(
                section_number=marker.section_number,
                section_title=marker.section_title,
                char_start=start,
                char_end=end,
                page_start=page_start,
                page_end=page_end,
            )
        )
    return sections


def persist_section_spans(
    db: Session,
    document_version_id: str,
    sections: Sequence[SectionSpan],
    *,
    replace_existing: bool = True,
) -> int:
    if not sections:
        return 0

    if replace_existing:
        db.execute(
            sqltext(
                """
                DELETE FROM document_sections
                WHERE document_version_id = :dvid
                """
            ),
            {"dvid": document_version_id},
        )

    for section in sections:
        db.execute(
            sqltext(
                """
                INSERT INTO document_sections (
                    document_version_id,
                    section_number,
                    section_title,
                    char_start,
                    char_end,
                    page_start,
                    page_end
                )
                VALUES (
                    :dvid,
                    :section_number,
                    :section_title,
                    :char_start,
                    :char_end,
                    :page_start,
                    :page_end
                )
                ON CONFLICT (document_version_id, section_number)
                DO UPDATE SET
                    section_title = EXCLUDED.section_title,
                    char_start = EXCLUDED.char_start,
                    char_end = EXCLUDED.char_end,
                    page_start = EXCLUDED.page_start,
                    page_end = EXCLUDED.page_end
                """
            ),
            {
                "dvid": document_version_id,
                "section_number": section.section_number,
                "section_title": section.section_title,
                "char_start": section.char_start,
                "char_end": section.char_end,
                "page_start": section.page_start,
                "page_end": section.page_end,
            },
        )

    db.commit()
    return len(sections)


def _find_section_markers(markdown: str) -> List[SectionMarker]:
    markers: List[SectionMarker] = []
    seen: set[str] = set()
    for match in SECTION_LINE_RE.finditer(markdown):
        line = match.group(0) or ""
        if _is_markdown_heading(line):
            continue
        if _looks_like_table_row(line):
            continue
        if _looks_like_toc_line(line):
            continue

        section_number = match.group("section")
        if not section_number or section_number in seen:
            continue
        tail = match.group("tail") or ""
        title = _clean_title(tail)
        if tail and title is None and any(ch.isalnum() for ch in tail):
            continue

        markers.append(
            SectionMarker(
                section_number=section_number,
                section_title=title,
                offset=match.start(),
            )
        )
        seen.add(section_number)

    return markers


def _find_page_markers(markdown: str) -> List[Tuple[int, int]]:
    markers: List[Tuple[int, int]] = []
    for pattern in PAGE_MARKER_RES:
        for match in pattern.finditer(markdown):
            page_value = match.group("page")
            if not page_value:
                continue
            try:
                page_num = int(page_value)
            except ValueError:
                continue
            markers.append((match.start(), page_num))
    markers.sort(key=lambda item: item[0])
    return markers


def _page_for_offset(markers: Sequence[Tuple[int, int]], offset: int) -> Optional[int]:
    if not markers:
        return None
    page = None
    for marker_offset, page_num in markers:
        if marker_offset > offset:
            break
        page = page_num
    return page


def _clean_title(tail: str) -> Optional[str]:
    if not tail:
        return None
    stripped = tail.strip()
    stripped = re.sub(r"^[\s\-\:\)\.]+", "", stripped)
    stripped = re.sub(r"\.{2,}\s*\d+\s*$", "", stripped)
    stripped = re.sub(r"\s{2,}", " ", stripped).strip()
    if not stripped:
        return None
    if not any(ch.isalpha() for ch in stripped):
        return None
    return stripped[:200]


def _is_markdown_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _looks_like_table_row(line: str) -> bool:
    return "|" in line and line.count("|") >= 2


def _looks_like_toc_line(line: str) -> bool:
    return re.search(r"\.{3,}\s*\d+\s*$", line.strip()) is not None
