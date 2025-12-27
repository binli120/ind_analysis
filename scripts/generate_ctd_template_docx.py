# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Generate CTD module DOCX templates from ind_24_26_template.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ncd.ctd_docx_template import write_module_docx, write_section_docx


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate-ctd-template-docx",
        description="Generate DOCX templates for CTD modules.",
    )
    parser.add_argument(
        "--module",
        help="Template module key (e.g., 2.4, 2.6-written, 2.6-tabulated).",
    )
    parser.add_argument(
        "--section",
        action="append",
        help="Section number to generate (repeatable, e.g., 2.6.1).",
    )
    parser.add_argument(
        "--sections",
        help="Comma-separated list of sections to generate.",
    )
    parser.add_argument(
        "--output-dir",
        help="Output directory for section templates (default: tmp/ctd-section-templates).",
    )
    parser.add_argument(
        "--output",
        default="tmp/ctd-2.4-template.docx",
        help="Output DOCX path (default: tmp/ctd-2.4-template.docx).",
    )
    parser.add_argument(
        "--template-path",
        help="Optional path to ind_24_26_template.json.",
    )
    return parser.parse_args(argv)


def _parse_sections(args: argparse.Namespace) -> list[str]:
    sections: list[str] = []
    if args.section:
        sections.extend(args.section)
    if args.sections:
        sections.extend([item.strip() for item in args.sections.split(",")])
    return [section.strip() for section in sections if section and section.strip()]


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    template_path = Path(args.template_path) if args.template_path else None
    sections = _parse_sections(args)

    if sections:
        output_dir = Path(args.output_dir or "tmp/ctd-section-templates")
        module_key = args.module
        for section in sections:
            output_path = output_dir / f"ctd-{section}-template.docx"
            write_section_docx(
                section_number=section,
                output_path=output_path,
                template_path=template_path,
                module_key=module_key,
            )
            print(f"[DOCX] Generated: {output_path}")
        return 0

    output_path = Path(args.output)
    module_key = args.module or "2.4"
    write_module_docx(
        module_key=module_key,
        output_path=output_path,
        template_path=template_path,
    )
    print(f"[DOCX] Generated: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
