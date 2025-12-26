# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Generate Section 2.6 summaries from Section 2.4 content using section_26_generation_template.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from summary.section26_generator import Section26Generator, Section26GeneratorConfig


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate-ind26-from-24",
        description="Generate Section 2.6 nonclinical summaries from Section 2.4 content using the provided template.",
    )
    parser.add_argument(
        "--input-24",
        required=True,
        help="Path to Section 2.4 content (markdown/text).",
    )
    parser.add_argument(
        "--output-json",
        required=False,
        help="Path to write generated Section 2.6 JSON (default: input path with .section_2_6.json).",
    )
    parser.add_argument(
        "--output-md",
        required=False,
        help="Path to write rendered Section 2.6 markdown (default: input path with .section_2_6.md).",
    )
    parser.add_argument(
        "--model",
        required=False,
        help="Override model (default: template model).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        required=False,
        help="Override temperature (default: template value).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    input_path = Path(args.input_24).expanduser()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")
    text = input_path.read_text(encoding="utf-8")

    output_json = Path(args.output_json).expanduser() if args.output_json else input_path.with_suffix(".section_2_6.json")
    output_md = Path(args.output_md).expanduser() if args.output_md else input_path.with_suffix(".section_2_6.md")

    config = Section26GeneratorConfig(
        output_json=output_json,
        output_markdown=output_md,
        model_override=args.model,
        temperature_override=args.temperature,
    )
    generator = Section26Generator(config)
    generator.generate(text)
    print(f"Wrote {output_json} and {output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
