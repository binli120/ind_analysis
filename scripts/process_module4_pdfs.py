# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Batch runner to pull Module 4 PDFs from S3, extract tables/images/text, and emit per-PDF folders."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Allow running as a script from repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from ncd.pipeline import run_pipeline


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Module 4 PDFs from S3 and extract markdown + DOCX with assets.",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("S3_BUCKET"),
        help="S3 bucket that contains Module 4 PDFs (default: S3_BUCKET env).",
    )
    parser.add_argument(
        "--prefix",
        required=True,
        help="S3 prefix under the bucket that points to Module 4 content (e.g. filynai.com/LT1009/Module 4).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="tmp/module4_extracted",
        help="Local directory where extracted folders will be written (default: tmp/module4_extracted).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on number of PDFs to process.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess even if an output folder for the PDF already exists.",
    )
    parser.add_argument(
        "--aws-region",
        default=os.getenv("AWS_REGION"),
        help="AWS region override (defaults to AWS_REGION env or boto3 default).",
    )
    return parser.parse_args(argv)


def iter_pdf_keys(client, bucket: str, prefix: str):
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".pdf") and not key.endswith("/"):
                yield key


def download_pdf(client, bucket: str, key: str, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(bucket, key, str(dest_path))


def process_pdf_key(client, bucket: str, key: str, output_root: Path, force: bool) -> None:
    pdf_stem = Path(key).stem
    pdf_output_dir = output_root / pdf_stem
    if pdf_output_dir.exists() and not force:
        print(f"[SKIP] {key} -> {pdf_output_dir} already exists (use --force to overwrite).")
        return

    pdf_output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        local_pdf = Path(tmpdir) / Path(key).name
        print(f"[DOWNLOAD] s3://{bucket}/{key} -> {local_pdf}")
        download_pdf(client, bucket, key, local_pdf)

        print(f"[PROCESS] {local_pdf} -> {pdf_output_dir}")
        run_pipeline(str(local_pdf), str(pdf_output_dir), output_basename=pdf_stem)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.bucket:
        print("S3 bucket is required (set --bucket or S3_BUCKET).", file=sys.stderr)
        return 1

    output_root = Path(args.output_dir).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)

    client = boto3.client("s3", region_name=args.aws_region)

    processed = 0
    try:
        for key in iter_pdf_keys(client, args.bucket, args.prefix):
            if args.limit is not None and processed >= args.limit:
                break
            try:
                process_pdf_key(client, args.bucket, key, output_root, args.force)
                processed += 1
            except ClientError as exc:
                print(f"[ERROR] Failed to process {key}: {exc}", file=sys.stderr)
    except ClientError as exc:
        print(f"[ERROR] Unable to list s3://{args.bucket}/{args.prefix}: {exc}", file=sys.stderr)
        return 1

    print(f"\nCompleted. PDFs processed: {processed}")
    print(f"Output root: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
