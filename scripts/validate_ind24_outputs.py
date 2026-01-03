# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Validate IND 2.4 generation outputs (gap analysis and summary)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence, Tuple

from summary import validate_gap_payload, validate_summary_payload

try:
    import boto3
    from botocore.exceptions import TokenRetrievalError  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional when using local files
    boto3 = None  # type: ignore[misc]
    TokenRetrievalError = Exception  # type: ignore[assignment]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="validate-ind24-outputs",
        description="Validate gap analysis and summary outputs produced by generate_ind24.py",
    )
    parser.add_argument(
        "--gap-json",
        required=False,
        help="Local path to gap_analysis JSON file (e.g., section_2_6_gap_analysis.json).",
    )
    parser.add_argument(
        "--gap-key",
        required=False,
        help="S3 key for gap_analysis JSON (use with --bucket). Example: filynai.com/LT1009/.../section_2_6_gap_analysis.json",
    )
    parser.add_argument(
        "--summary-json",
        required=False,
        help="Local path to summary JSON. If omitted, summary validation is skipped.",
    )
    parser.add_argument(
        "--summary-key",
        required=False,
        help="S3 key for summary JSON (use with --bucket).",
    )
    parser.add_argument(
        "--bucket",
        required=False,
        help="S3 bucket name when loading from S3. Omit for local files.",
    )
    parser.add_argument(
        "--chunk-count",
        type=int,
        default=1,
        help="Chunk count used during generation (for scoring context).",
    )
    return parser.parse_args(argv)


def _parse_s3_uri(value: str) -> Tuple[str, str]:
    if not value.startswith("s3://"):
        raise ValueError("S3 URI must start with s3://")
    _, _, remainder = value.partition("s3://")
    bucket, _, key = remainder.partition("/")
    if not bucket or not key:
        raise ValueError("Invalid S3 URI; expected s3://bucket/key")
    return bucket, key


def _load_json(source: str, *, bucket: str | None = None) -> Any:
    # Explicit S3 when using s3:// URI.
    if source.startswith("s3://"):
        bucket, key = _parse_s3_uri(source)
        if boto3 is None:
            raise SystemExit(
                "boto3 is required for S3 validation. Install infra extras."
            )
        s3 = boto3.client("s3")
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = obj["Body"].read().decode("utf-8")
            return json.loads(data)
        except TokenRetrievalError as exc:
            raise SystemExit(
                "Failed to refresh AWS SSO/token. Please re-authenticate "
                "(e.g., aws sso login) and retry."
            ) from exc
        except Exception as exc:
            raise SystemExit(
                f"Failed to load S3 object s3://{bucket}/{key}: {exc}"
            ) from exc

    # Otherwise treat as local path only.
    local_path = Path(source).expanduser()
    if not local_path.exists():
        raise SystemExit(f"Local file not found: {local_path}")
    return json.loads(local_path.read_text(encoding="utf-8"))


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    gap_source = args.gap_json or args.gap_key
    if not gap_source:
        raise SystemExit("Provide --gap-json (local) or --gap-key (local or s3://...)")
    gap_payload = _load_json(gap_source, bucket=args.bucket)
    gap_report = validate_gap_payload(gap_payload, chunk_count=args.chunk_count)
    print("Gap Analysis Validation")
    print("=======================")
    print("Thinking:")
    for evidence in gap_report.get("evidence", []):
        print(f"- {evidence}")
    print("Issues:")
    for issue in gap_report.get("issues", []):
        print(f"- {issue}")
    print("Confidence:", gap_report.get("confidence"))

    summary_source = args.summary_json or args.summary_key
    if summary_source:
        summary_payload = _load_json(summary_source, bucket=args.bucket)
        summary_report = validate_summary_payload(summary_payload)
        print("\nSummary Validation")
        print("==================")
        print("Thinking:")
        for evidence in summary_report.get("evidence", []):
            print(f"- {evidence}")
        print("Issues:")
        for issue in summary_report.get("issues", []):
            print(f"- {issue}")
        print("Confidence:", summary_report.get("confidence"))
    else:
        print("\nSummary Validation skipped (no summary JSON provided).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
