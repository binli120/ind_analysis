# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Utility script for exporting the API's OpenAPI document."""

# author: Bin Lee
# email: blee@filynai.com

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from pdf_analysis.api.server import app


def main() -> int:
    """Render the FastAPI OpenAPI schema to disk."""
    parser = argparse.ArgumentParser(
        description="Generate the OpenAPI specification for the PDF Analysis API.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist/openapi.json"),
        help="Destination path for the OpenAPI JSON (default: dist/openapi.json).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation (default: 2).",
    )
    args = parser.parse_args()

    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    spec = app.openapi()
    output_path.write_text(json.dumps(spec, indent=args.indent), encoding="utf-8")
    logging.info("OpenAPI spec written to %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
