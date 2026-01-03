# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Generate Section 2.6 summaries from Section 2.4 content stored in S3."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import boto3

from summary.section26_generator import (
    Section26Generator,
    Section26GeneratorConfig,
    _render_section26_markdown,
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate-ind26",
        description="Generate Section 2.6 written summaries from Section 2.4 content stored in S3.",
    )
    parser.add_argument(
        "--bucket", required=True, help="S3 bucket containing project folders."
    )
    parser.add_argument(
        "--company",
        default="filynai.com",
        help="Top-level company prefix in S3 (default: filynai.com).",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project folder (e.g., LT1009).",
    )
    parser.add_argument(
        "--section-prefix",
        help="Optional explicit 2.4 prefix. If omitted, auto-discovers a prefix containing '2.4'.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(levelname)s %(message)s"
    )
    s3 = boto3.client("s3")

    prefix_24 = args.section_prefix or _discover_24_prefix(
        s3, args.bucket, args.company, args.project
    )
    if not prefix_24:
        raise SystemExit(
            "Unable to find a 2.4 prefix. Provide --section-prefix explicitly."
        )

    if "2.6" in prefix_24.lower():
        raise SystemExit(
            "The prefix appears to be a 2.6 path; please supply the 2.4 folder."
        )

    logging.info("Using 2.4 prefix: %s", prefix_24)
    objects_24 = _list_objects(s3, args.bucket, prefix_24)
    md_keys = [obj for obj in objects_24 if obj.lower().endswith(".md")]
    if not md_keys:
        raise SystemExit("No .md files found in the 2.4 prefix; cannot proceed.")

    texts: List[str] = []
    for key in md_keys:
        try:
            body = (
                s3.get_object(Bucket=args.bucket, Key=key)["Body"]
                .read()
                .decode("utf-8")
            )
            texts.append(f"# Source: {key}\n\n{body}")
        except Exception as exc:
            logging.warning("Failed to read %s: %s", key, exc)
    if not texts:
        raise SystemExit("Unable to load any Section 2.4 markdown content.")

    section24_text = "\n\n".join(texts)

    generator = Section26Generator(Section26GeneratorConfig())
    payload = generator.generate(section24_text)
    markdown = _render_section26_markdown(payload)

    output_prefix = _derive_26_prefix(prefix_24)
    json_key = f"{output_prefix}/section_2_6.json"
    md_key = f"{output_prefix}/section_2_6.md"

    s3.put_object(
        Bucket=args.bucket,
        Key=json_key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    s3.put_object(
        Bucket=args.bucket,
        Key=md_key,
        Body=markdown.encode("utf-8"),
        ContentType="text/markdown",
    )
    logging.info("Wrote %s and %s", json_key, md_key)
    return 0


def _discover_24_prefix(
    s3: any, bucket: str, company: str, project: str
) -> Optional[str]:
    base_prefix = f"{company.rstrip('/')}/{project}/"
    candidates: Dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix):
        for entry in page.get("Contents", []):
            key = entry.get("Key")
            if not key:
                continue
            if "2.4" in key.lower():
                # take folder up to the "2.4" segment
                parts = key.split("/")
                for idx, part in enumerate(parts):
                    if "2.4" in part.lower():
                        prefix = "/".join(parts[: idx + 1])
                        candidates[prefix] = candidates.get(prefix, 0) + 1
                        break
    if not candidates:
        return None
    # pick prefix with most hits
    return sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)[0][0]


def _list_objects(s3: any, bucket: str, prefix: str) -> List[str]:
    keys: List[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []):
            key = entry.get("Key")
            if key:
                keys.append(key)
    return keys


def _derive_26_prefix(prefix_24: str) -> str:
    lower = prefix_24.lower()
    if "2.4" in lower:
        return prefix_24.replace("2.4", "2.6", 1)
    return f"{prefix_24.rstrip('/')}/section_2_6"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
