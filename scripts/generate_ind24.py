"""CLI helper to generate IND Section 2.4 from Section 2.6 content in S3."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Sequence

from summary import IND24GenerationConfig, IND24GenerationPipeline

try:  # optional convenience for local env files
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    load_dotenv = None


def _load_env_files() -> None:
    """Load .env and .env.local when python-dotenv is available."""
    if load_dotenv is None:
        return
    for candidate in (Path(".env.local"), Path(".env")):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate-ind24",
        description="Generate IND Section 2.4 summary from Section 2.6 content stored in S3.",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("S3_BUCKET"),
        help="S3 bucket containing the Section 2.6 folder (env: S3_BUCKET).",
    )
    parser.add_argument(
        "--company",
        default=os.getenv("COMPANY", "filynai.com"),
        help="Top-level company prefix (default: filynai.com).",
    )
    parser.add_argument(
        "--project",
        default=os.getenv("PROJECT", "LT1009"),
        help="Project folder (default: LT1009).",
    )
    parser.add_argument(
        "--section-prefix",
        default=os.getenv("SECTION_PREFIX"),
        help=(
            "Prefix to the Section 2.6 folder in S3. "
            "If omitted, the script will auto-discover folders containing '2.6' under the project."
        ),
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL"),
        help="Redis URL for cached markdown sidecars (optional).",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=None,
        help="Limit number of Section 2.6 documents to process (for quick tests).",
    )
    parser.add_argument(
        "--gap-model",
        default=os.getenv("GAP_MODEL", "gpt-4o-mini"),
        help="OpenAI model for gap analysis (default: gpt-4o-mini).",
    )
    parser.add_argument(
        "--summary-model",
        default=os.getenv("SUMMARY_MODEL"),
        help="Override model for the 2.4 summary (optional; falls back to template default).",
    )
    parser.add_argument(
        "--output-combined",
        default=os.getenv("OUTPUT_COMBINED"),
        help="Override S3 key for combined markdown output.",
    )
    parser.add_argument(
        "--output-gap",
        default=os.getenv("OUTPUT_GAP"),
        help="Override S3 key for gap analysis JSON output.",
    )
    parser.add_argument(
        "--output-summary",
        default=os.getenv("OUTPUT_SUMMARY"),
        help="Override S3 key for Section 2.4 summary JSON output.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO).",
    )
    parser.add_argument(
        "--debug-list",
        action="store_true",
        help="List the first few keys under the Section 2.6 prefix and exit (for troubleshooting).",
    )
    parser.add_argument(
        "--auto-discover-prefix",
        action="store_true",
        default=os.getenv("AUTO_DISCOVER_PREFIX", "true").lower() == "true",
        help="Auto-discover the Section 2.6 prefix under company/project if the provided prefix has no keys.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    _load_env_files()
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(message)s")

    if not args.bucket:
        raise SystemExit("S3 bucket is required (set --bucket or S3_BUCKET env).")

    section_prefix = args.section_prefix
    if section_prefix:
        section_prefix = re.sub(r"/\s+", "/", re.sub(r"\s+", " ", section_prefix)).strip()

    if args.debug_list:
        target_prefix = section_prefix or f"{args.company.rstrip('/')}/{args.project}/"
        _debug_list_prefix(args.bucket, target_prefix)
        return 0

    sample_keys: list[str] = []
    if section_prefix:
        sample_keys = _ensure_prefix_has_objects(args.bucket, section_prefix, limit=5)

    if (not sample_keys) and args.auto_discover_prefix:
        discovered = _discover_section_prefix(args.bucket, args.company, args.project)
        if discovered:
            section_prefix = discovered[0][0]
            logging.info(
                "Auto-discovered Section 2.6 prefix: %s (matches=%d)",
                section_prefix,
                discovered[0][1],
            )
            sample_keys = _ensure_prefix_has_objects(args.bucket, section_prefix, limit=5)
        else:
            logging.warning(
                "Unable to auto-discover a Section 2.6 prefix under %s/%s. "
                "Use --debug-list with a broader prefix.",
                args.company,
                args.project,
            )

    if not sample_keys:
        raise SystemExit(
            "No Section 2.6 objects found. Provide a correct --section-prefix or keep "
            "--auto-discover-prefix enabled to locate folders containing '2.6'."
        )

    config = IND24GenerationConfig(
        bucket=args.bucket,
        project=args.project,
        company=args.company,
        section_prefix=section_prefix,
        redis_url=args.redis_url,
        max_documents=args.max_documents,
        gap_model=args.gap_model,
        summary_model_override=args.summary_model,
        output_combined_markdown_key=args.output_combined,
        output_gap_key=args.output_gap,
        output_summary_key=args.output_summary,
    )

    logging.info("Starting IND 2.4 generation for project=%s prefix=%s", args.project, section_prefix)
    result = IND24GenerationPipeline(config).run()
    output_keys = result.get("output_keys", {})
    logging.info("Completed. Outputs: %s", output_keys)
    print(json.dumps(output_keys, indent=2))
    return 0


def _debug_list_prefix(bucket: str, prefix: str, limit: int = 20) -> None:
    """List a few keys under the prefix to confirm the path is correct."""
    try:
        import boto3
    except ModuleNotFoundError:
        raise SystemExit("boto3 is required for --debug-list.")

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    print(f"Listing up to {limit} keys under s3://{bucket}/{prefix} ...")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []):
            print(entry.get("Key"))
            count += 1
            if count >= limit:
                return
    if count == 0:
        print("No keys found under that prefix.")


def _ensure_prefix_has_objects(bucket: str, prefix: str, limit: int = 5) -> list[str]:
    """Return up to `limit` keys under the prefix to confirm existence."""
    try:
        import boto3
    except ModuleNotFoundError:
        return []

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig={"MaxItems": limit}):
        for entry in page.get("Contents", []):
            key = entry.get("Key")
            if key:
                keys.append(key)
                if len(keys) >= limit:
                    return keys
    return keys


def _discover_section_prefix(bucket: str, company: str, project: str, max_items: int = 400) -> list[tuple[str, int]]:
    """
    Find likely Section 2.6 prefixes under company/project by scanning keys containing '2.6'.
    Returns a list of (prefix, match_count) sorted by count desc.
    """
    try:
        import boto3
    except ModuleNotFoundError:
        return []

    base_prefix = f"{company.rstrip('/')}/{project}/"
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    candidates: dict[str, int] = {}
    pattern = re.compile(r"2\.6", re.IGNORECASE)
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=base_prefix,
        PaginationConfig={"MaxItems": max_items},
    ):
        for entry in page.get("Contents", []):
            key = entry.get("Key")
            if not key:
                continue
            segments = key.split("/")
            for idx, segment in enumerate(segments):
                if pattern.search(segment):
                    candidate = "/".join(segments[: idx + 1])
                    candidates[candidate] = candidates.get(candidate, 0) + 1
                    break
    return sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
