import re
from typing import List, cast

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

SECTION_REGEX = re.compile(r"\b4\.[0-9](?:\.[0-9]+){0,2}\b")

TOPIC_RULES = {
    "hepatotoxicity": ["liver", "hepat", "alt", "ast", "bilirubin"],
    "nephrotoxicity": ["kidney", "renal", "creatinine"],
    "qt_prolongation": ["qt", "ecg", "herg"],
    "immunotoxicity": ["lymph", "cytokine", "immune"],
    "pharmacokinetics": ["auc", "cmax", "clearance", "t1/2", "half-life"],
}


def detect_section_numbers(text: str) -> List[str]:
    return list(set(SECTION_REGEX.findall(text)))


def extract_section_title(text: str, section_number: str | None) -> str | None:
    if not section_number:
        return None
    idx = text.find(section_number)
    if idx == -1:
        return None
    after = text[idx + len(section_number) :]
    title = after.split("\n", 1)[0].strip()
    return title[:200]


def detect_topics(text: str) -> List[str]:
    t = text.lower()
    found = []
    for topic, kws in TOPIC_RULES.items():
        if any(kw in t for kw in kws):
            found.append(topic)
    return found


def ensure_topic_exists(db: Session, topic: str) -> str:
    row = db.execute(
        sqltext("SELECT id FROM ncd_topic WHERE name = :t"), {"t": topic}
    ).scalar()

    if row is not None:
        return cast(str, row)

    new_id = db.execute(
        sqltext("""
            INSERT INTO ncd_topic (name)
            VALUES (:t)
            RETURNING id;
        """),
        {"t": topic},
    ).scalar()

    if new_id is None:
        raise RuntimeError("Failed to create topic record")

    db.commit()
    return cast(str, new_id)


def chunk_text_by_pages(
    db: Session, source_document_id: str, max_chars: int = 1500
) -> List[str]:
    # Cleanup existing data
    db.execute(
        sqltext("""
            DELETE FROM ncd_text_chunk_embedding
            WHERE chunk_id IN (
                SELECT id FROM ncd_text_chunk WHERE source_document_id = :sid
            );
        """),
        {"sid": source_document_id},
    )
    db.execute(
        sqltext("""
            DELETE FROM ncd_text_chunk
            WHERE source_document_id = :sid
        """),
        {"sid": source_document_id},
    )
    db.commit()

    # Fetch pages
    pages = (
        db.execute(
            sqltext("""
            SELECT page_number, text
            FROM ncd_document_page
            WHERE source_document_id = :sid
            ORDER BY page_number
        """),
            {"sid": source_document_id},
        )
        .mappings()
        .all()
    )

    chunks: list[tuple[int, int, str]] = []
    buffer = ""
    start_page: int | None = None

    for p in pages:
        txt = p["text"] or ""
        paras = [t.strip() for t in txt.split("\n\n") if t.strip()]
        for para in paras:
            if not buffer:
                start_page = p["page_number"]
            if len(buffer) + len(para) > max_chars:
                if start_page is None:
                    start_page = p["page_number"]
                chunks.append((start_page, p["page_number"], buffer))
                buffer = para + "\n\n"
                start_page = p["page_number"]
            else:
                buffer += para + "\n\n"

    if buffer:
        end_page = pages[-1]["page_number"]
        if start_page is None:
            start_page = end_page
        chunks.append((start_page, end_page, buffer))

    chunk_ids: list[str] = []

    # Insert chunks + topics + section info
    for pf, pt, text_block in chunks:
        sections = detect_section_numbers(text_block)
        primary = sections[0] if sections else None
        title = extract_section_title(text_block, primary)
        topics = detect_topics(text_block)

        extra = {
            "sections": sections,
            "section_title": title,
            "topics": topics,
        }

        cid = db.execute(
            sqltext("""
                INSERT INTO ncd_text_chunk (
                    source_document_id, page_from, page_to,
                    raw_text, section_label, extra_attributes
                ) VALUES (
                    :sid, :pf, :pt, :txt, :sec, :extra
                )
                RETURNING id
            """),
            {
                "sid": source_document_id,
                "pf": pf,
                "pt": pt,
                "txt": text_block,
                "sec": primary,
                "extra": extra,
            },
        ).scalar()

        if cid is None:
            raise RuntimeError("Failed to insert text chunk")

        # store topic assignments
        for topic in topics:
            tid = ensure_topic_exists(db, topic)
            db.execute(
                sqltext("""
                    INSERT INTO ncd_topic_assignment (topic_id, chunk_id)
                    VALUES (:tid, :cid)
                """),
                {"tid": tid, "cid": cid},
            )

        chunk_ids.append(cast(str, cid))

    db.commit()
    return chunk_ids
