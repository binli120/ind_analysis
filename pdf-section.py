# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from pathlib import Path

from ncd.ingestion.section_detector import extract_section_spans

md_path = Path("tmp/out/4.2.1.1.pipeline.md")
markdown = md_path.read_text(encoding="utf-8")
sections = extract_section_spans(markdown)
if not sections:
    print("No section numbers detected.")
else:
    for section in sections:
        title = section.section_title or ""
        label = f"{section.section_number} {title}".strip()
        print(f"- {label}")
