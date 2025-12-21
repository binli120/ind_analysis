#!/usr/bin/env python
"""
Batch runner to ingest Module 4 PDFs from S3 into the NCD database.

This script can orchestrate two pipelines per PDF:
  - Core ingestion (S3 -> markdown/quality + documents/document_versions + ingestion status)
  - NCD full tox pipeline (source documents + findings/exposure/safety summary)

Routing logic (auto mode):
  - If markdown sidecar is missing (or ingestion status incomplete), run core.
  - If tox data is missing in DB, run the NCD tox pipeline.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import text as sqltext

# Allow running from repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from ncd.db_interface import NCDRepository
from ncd.llm_client import LLMClient
from ncd.pipeline_runner import run_pdf_ingest_and_extract
from ncd.ingestion.pdf_ingestion import sha256_file
from sqs_worker import process_message


_MODULE_PATTERN = re.compile(r"^module\s*(?P<number>\d+)(?:[\s._-].*)?$", re.IGNORECASE)


class DummyLLM(LLMClient):
    """Deterministic stub for tox pipeline testing (no external calls)."""

    def extract_json(self, system_prompt: str, user_prompt: str, response_model=None):  # type: ignore[override]
        lower_prompt = f"{system_prompt} {user_prompt}".lower()
        if "pk" in lower_prompt or "cmax" in lower_prompt or "auc" in lower_prompt:
            return {
                "study_id": "demo",
                "species": "rat",
                "route": "oral",
                "parameters": [
                    {
                        "dose_group_name": "High",
                        "parameter": "Cmax",
                        "value": 123.4,
                        "unit": "ng/mL",
                        "timepoint": "Day 1",
                        "clinical_multiple": 4.2,
                    },
                    {
                        "dose_group_name": "High",
                        "parameter": "AUC",
                        "value": 9876.5,
                        "unit": "ng*h/mL",
                        "timepoint": "Day 28",
                        "clinical_multiple": 3.8,
                    },
                ],
                "source_chunk_ids": [],
            }

        return {
            "study_id": "demo",
            "species": "rat",
            "route": "oral",
            "duration_days": 28,
            "noael_mg_per_kg": 50,
            "loael_mg_per_kg": 100,
            "limiting_organ": "liver",
            "limiting_finding": "ALT increase",
            "clinical_multiple": 5,
            "dose_groups": [
                {
                    "name": "Low",
                    "sex": "M/F",
                    "n_animals": 10,
                    "dose_mg_per_kg": 10,
                    "dose_mg_per_m2": None,
                },
                {
                    "name": "High",
                    "sex": "M/F",
                    "n_animals": 10,
                    "dose_mg_per_kg": 50,
                    "dose_mg_per_m2": None,
                },
            ],
            "exposure_metrics": [
                {
                    "dose_group_name": "High",
                    "parameter": "Cmax",
                    "value": 123.4,
                    "unit": "ng/mL",
                    "timepoint": "Day 1",
                    "clinical_multiple": 4.2,
                },
                {
                    "dose_group_name": "High",
                    "parameter": "AUC",
                    "value": 9876.5,
                    "unit": "ng*h/mL",
                    "timepoint": "Day 28",
                    "clinical_multiple": 3.8,
                },
            ],
            "findings": [
                {
                    "organ_system": "Hepatic",
                    "organ": "Liver",
                    "finding_term": "ALT increase",
                    "severity": "mild",
                    "adverse": False,
                    "reversible": True,
                    "onset_day": 7,
                    "dose_threshold_mg_per_kg": 50,
                    "noael_flag": False,
                }
            ],
            "source_chunk_ids": [],
        }

    def generate_text(self, system_prompt: str, user_prompt: str) -> str:  # type: ignore[override]
        return "repeat_dose_tox"


def _module_number(segment: str) -> Optional[int]:
    match = _MODULE_PATTERN.match(segment.strip())
    if not match:
        return None
    try:
        return int(match.group("number"))
    except (TypeError, ValueError):
        return None


def _normalize_version_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    value_str = str(value).strip()
    if not value_str or value_str.lower() == "null":
        return None
    return value_str


def _to_iso(dt: Any) -> Optional[str]:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return None


def _is_pdf_key(key: str) -> bool:
    return key.lower().endswith(".pdf") and not key.endswith("/")


def _iter_latest_pdf_versions(
    client: Any, bucket: str, prefix: str
) -> Iterator[Dict[str, Any]]:
    try:
        paginator = client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for version in page.get("Versions", []):
                if not version.get("IsLatest"):
                    continue
                key = version.get("Key")
                if not key or not _is_pdf_key(key):
                    continue
                yield {
                    "key": key,
                    "version_id": _normalize_version_id(version.get("VersionId")),
                    "last_modified": _to_iso(version.get("LastModified")),
                }
        return
    except ClientError as exc:
        print(
            f"[WARN] list_object_versions failed ({exc}); falling back to list_objects_v2.",
            file=sys.stderr,
        )

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key or not _is_pdf_key(key):
                continue
            yield {
                "key": key,
                "version_id": None,
                "last_modified": _to_iso(obj.get("LastModified")),
            }


def _iter_module_pdfs(
    client: Any,
    bucket: str,
    prefix: str,
    module_number: int,
) -> Iterator[Dict[str, Any]]:
    for obj in _iter_latest_pdf_versions(client, bucket, prefix):
        parts = obj["key"].split("/")
        if len(parts) < 3:
            continue
        mod_num = _module_number(parts[2])
        if mod_num != module_number:
            continue
        yield obj


def _format_error(exc: Exception, limit: int = 2000) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if len(message) > limit:
        return f"{message[:limit - 3]}..."
    return message


def _s3_object_exists(client: Any, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return False
        raise


def _download_s3_pdf(
    client: Any, bucket: str, key: str, version_id: Optional[str]
) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    extra_args = {"VersionId": version_id} if version_id else None
    client.download_file(Bucket=bucket, Key=key, Filename=tmp.name, ExtraArgs=extra_args)
    return Path(tmp.name)


def _has_tox_data(repo: NCDRepository, project_id: str, file_name: str) -> bool:
    row = repo.session.execute(
        sqltext(
            """
            SELECT EXISTS (
                SELECT 1
                FROM ncd_source_document sd
                JOIN ncd_study s ON s.main_source_document_id = sd.id
                LEFT JOIN ncd_finding f ON f.study_id = s.id
                LEFT JOIN ncd_exposure_metric em ON em.study_id = s.id
                LEFT JOIN ncd_study_safety_summary ss ON ss.study_id = s.id
                WHERE sd.project_id = :pid
                  AND sd.file_name = :fname
                  AND (f.id IS NOT NULL OR em.id IS NOT NULL OR ss.id IS NOT NULL)
            ) AS has_data
            """
        ),
        {"pid": project_id, "fname": file_name},
    ).scalar()
    return bool(row)


def _resolve_llm(mode: str) -> LLMClient:
    if mode == "dummy":
        return DummyLLM()
    if mode == "real":
        return LLMClient()
    raise ValueError("tox llm mode must be 'real' or 'dummy'")


def _core_needed(
    *,
    core_check: str,
    md_exists: bool,
    status_completed: bool,
    force: bool,
) -> bool:
    if force:
        return True
    if core_check == "md":
        return not md_exists
    if core_check == "status":
        return not status_completed
    if core_check == "both":
        return (not md_exists) or (not status_completed)
    return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch ingest Module 4 PDFs from S3 into the DB.")
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET"), help="S3 bucket name.")
    parser.add_argument("--company", default=os.getenv("COMPANY", "filynai.com"), help="Company prefix.")
    parser.add_argument("--project", default=os.getenv("PROJECT"), help="Project name under company.")
    parser.add_argument("--project-id", default=os.getenv("PROJECT_ID"), help="Project UUID for NCD tox pipeline.")
    parser.add_argument("--module", type=int, default=4, help="Module number to ingest.")
    parser.add_argument(
        "--prefix",
        default=None,
        help="Override S3 prefix (defaults to <company>/<project>/).",
    )
    parser.add_argument("--aws-region", default=os.getenv("AWS_REGION"), help="AWS region override.")
    parser.add_argument("--tenant-id", default=os.getenv("DEFAULT_TENANT_ID"), help="Tenant UUID.")
    parser.add_argument("--created-by", default=os.getenv("DEFAULT_USER_ID"), help="User UUID.")
    parser.add_argument("--limit", type=int, default=None, help="Limit on number of PDFs to process.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent worker threads.")
    parser.add_argument("--mode", choices=("auto", "core", "tox"), default="auto")
    parser.add_argument("--core-check", choices=("md", "status", "both"), default="both")
    parser.add_argument("--md-suffix", default=".extracted.md", help="Markdown sidecar suffix.")
    parser.add_argument(
        "--skip-langchain",
        action="store_true",
        help="Skip LangChain pipeline even when core runs.",
    )
    parser.add_argument(
        "--force-langchain",
        action="store_true",
        help="Re-run LangChain pipeline even if completed.",
    )
    parser.add_argument(
        "--core-text-engines",
        default=None,
        help="Comma-separated text engines for core pipeline (e.g., pymupdf,pdfminer).",
    )
    parser.add_argument(
        "--core-table-engines",
        default=None,
        help="Comma-separated table engines for core pipeline (e.g., pdfplumber,camelot).",
    )
    parser.add_argument(
        "--disable-core-tables",
        action="store_true",
        help="Disable table extraction in core pipeline (avoids camelot/tabula).",
    )
    parser.add_argument("--force", action="store_true", help="Alias for --force-core.")
    parser.add_argument("--force-core", action="store_true", help="Re-run core pipeline regardless of checks.")
    parser.add_argument("--force-tox", action="store_true", help="Re-run tox pipeline regardless of checks.")
    parser.add_argument("--no-precheck", action="store_true", help="Disable ingestion status precheck.")
    parser.add_argument("--dry-run", action="store_true", help="List matches without processing.")
    parser.add_argument(
        "--tox-llm",
        choices=("real", "dummy"),
        default="real",
        help="LLM mode for tox pipeline.",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embeddings in tox pipeline.",
    )
    parser.add_argument(
        "--report-dir",
        default="tmp/ingestion_reports",
        help="Directory to write JSON/CSV reports.",
    )
    return parser.parse_args(argv)


def _process_document(obj: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    key = obj["key"]
    version_id = obj.get("version_id")
    last_modified = obj.get("last_modified")
    file_name = Path(key).name
    md_key = f"{key}{args.md_suffix}"

    s3_client = boto3.client("s3", region_name=args.aws_region)
    repo: Optional[NCDRepository] = None
    md_exists = False
    status_completed = False
    tox_data_exists = False

    core_required = False
    langchain_required = False
    tox_required = False

    core_status = "skipped"
    langchain_status = "skipped"
    tox_status = "skipped"
    core_error = ""
    langchain_error = ""
    tox_error = ""
    core_duration = 0.0
    langchain_duration = 0.0
    tox_duration = 0.0
    document_version_id = ""
    tox_source_document_id = ""
    tox_study_id = ""

    try:
        precheck_enabled = not args.no_precheck

        if args.mode in {"auto", "core"}:
            try:
                md_exists = _s3_object_exists(s3_client, args.bucket, md_key)
            except Exception as exc:
                md_exists = False
                core_error = _format_error(exc)

            if precheck_enabled:
                try:
                    repo = repo or NCDRepository()
                    core_status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="core",
                    )
                    langchain_status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="langchain",
                    )
                    status_completed = bool(
                        core_status_row and core_status_row.get("status") == "completed"
                    )
                    if core_status_row and core_status_row.get("document_version_id"):
                        document_version_id = str(core_status_row.get("document_version_id"))
                    if not document_version_id and langchain_status_row and langchain_status_row.get("document_version_id"):
                        document_version_id = str(langchain_status_row.get("document_version_id"))
                    if core_status_row:
                        core_status = str(core_status_row.get("status") or core_status)
                    if langchain_status_row:
                        langchain_status = str(langchain_status_row.get("status") or langchain_status)
                except Exception as exc:
                    status_completed = False
                    core_error = _format_error(exc)

            core_required = _core_needed(
                core_check=args.core_check,
                md_exists=md_exists,
                status_completed=status_completed,
                force=bool(args.force or args.force_core),
            )

            if args.skip_langchain:
                langchain_required = False
            else:
                langchain_required = bool(args.force_langchain) or langchain_status != "completed"

            if langchain_required and not document_version_id:
                core_required = True

        if args.mode in {"auto", "tox"}:
            if not args.project_id:
                raise RuntimeError("project_id is required for tox pipeline checks")
            try:
                repo = repo or NCDRepository()
                tox_status_row = repo.fetch_pipeline_status_for_key(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    pipeline="tox",
                )
                if tox_status_row:
                    tox_status = str(tox_status_row.get("status") or tox_status)
                tox_data_exists = _has_tox_data(repo, args.project_id, file_name)
                tox_required = args.force_tox or tox_status != "completed" or not tox_data_exists
            except Exception as exc:
                tox_data_exists = False
                tox_required = True
                tox_error = _format_error(exc)

        if args.dry_run:
            return {
                "key": key,
                "version_id": version_id,
                "last_modified": last_modified,
                "md_exists": md_exists,
                "status_completed": status_completed,
                "tox_data_exists": tox_data_exists,
                "core_required": core_required,
                "core_status": "dry_run",
                "core_error": core_error,
                "core_duration_seconds": core_duration,
                "langchain_required": langchain_required,
                "langchain_status": "dry_run",
                "langchain_error": langchain_error,
                "langchain_duration_seconds": langchain_duration,
                "document_version_id": document_version_id,
                "tox_required": tox_required,
                "tox_status": "dry_run",
                "tox_error": tox_error,
                "tox_duration_seconds": tox_duration,
                "tox_source_document_id": tox_source_document_id,
                "tox_study_id": tox_study_id,
            }

        if (core_required or langchain_required) and args.mode in {"auto", "core"}:
            started = datetime.now(timezone.utc)
            try:
                result = process_message(
                    {
                        "bucket": args.bucket,
                        "key": key,
                        "version_id": version_id,
                        "tenant_id": args.tenant_id,
                        "created_by": args.created_by,
                    },
                    force=bool(args.force or args.force_core),
                    run_core=core_required,
                    run_langchain=langchain_required,
                )
                core_status = result.get("core_status", core_status)
                langchain_status = result.get("langchain_status", langchain_status)
                core_error = result.get("core_error") or core_error
                langchain_error = result.get("langchain_error") or langchain_error
                core_duration = float(result.get("core_duration_seconds") or core_duration)
                langchain_duration = float(result.get("langchain_duration_seconds") or langchain_duration)
                if result.get("document_version_id"):
                    document_version_id = str(result.get("document_version_id"))
            except Exception as exc:
                if core_required:
                    core_status = "failed"
                    core_error = _format_error(exc)
                if langchain_required:
                    langchain_status = "failed"
                    langchain_error = _format_error(exc)
            core_duration = max(core_duration, (datetime.now(timezone.utc) - started).total_seconds())

        if tox_required and args.mode in {"auto", "tox"}:
            started = datetime.now(timezone.utc)
            local_pdf = None
            try:
                llm_client = _resolve_llm(args.tox_llm)
                local_pdf = _download_s3_pdf(s3_client, args.bucket, key, version_id)
                content_hash = sha256_file(local_pdf)
                repo = repo or NCDRepository()
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="tox",
                    status="processing",
                )
                tox_result = run_pdf_ingest_and_extract(
                    pdf_path=local_pdf,
                    project_id=args.project_id,
                    module="Module 4",
                    llm_client=llm_client,
                    embed=not args.no_embed,
                )
                tox_status = "completed"
                tox_source_document_id = str(tox_result.get("source_document_id", ""))
                tox_study_id = str(tox_result.get("study_id", ""))
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="tox",
                    status="completed",
                    source_document_id=tox_source_document_id,
                    study_id=tox_study_id,
                )
            except Exception as exc:
                tox_status = "failed"
                tox_error = _format_error(exc)
                if local_pdf:
                    try:
                        content_hash = sha256_file(local_pdf)
                        repo = repo or NCDRepository()
                        repo.upsert_pipeline_status(
                            s3_bucket=args.bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="tox",
                            status="failed",
                            error_message=tox_error,
                        )
                    except Exception:
                        pass
            finally:
                if local_pdf and local_pdf.exists():
                    try:
                        local_pdf.unlink()
                    except Exception:
                        pass
            tox_duration = (datetime.now(timezone.utc) - started).total_seconds()
        return {
            "key": key,
            "version_id": version_id,
            "last_modified": last_modified,
            "md_exists": md_exists,
            "status_completed": status_completed,
            "tox_data_exists": tox_data_exists,
            "core_required": core_required,
            "core_status": core_status,
            "core_error": core_error,
            "core_duration_seconds": round(core_duration, 3),
            "langchain_required": langchain_required,
            "langchain_status": langchain_status,
            "langchain_error": langchain_error,
            "langchain_duration_seconds": round(langchain_duration, 3),
            "document_version_id": document_version_id,
            "tox_required": tox_required,
            "tox_status": tox_status,
            "tox_error": tox_error,
            "tox_duration_seconds": round(tox_duration, 3),
            "tox_source_document_id": tox_source_document_id,
            "tox_study_id": tox_study_id,
        }
    except Exception as exc:
        core_error = core_error or _format_error(exc)
        return {
            "key": key,
            "version_id": version_id,
            "last_modified": last_modified,
            "md_exists": md_exists,
            "status_completed": status_completed,
            "tox_data_exists": tox_data_exists,
            "core_required": core_required,
            "core_status": "failed",
            "core_error": core_error,
            "core_duration_seconds": round(core_duration, 3),
            "langchain_required": langchain_required,
            "langchain_status": "failed" if langchain_required else langchain_status,
            "langchain_error": langchain_error,
            "langchain_duration_seconds": round(langchain_duration, 3),
            "document_version_id": document_version_id,
            "tox_required": tox_required,
            "tox_status": "failed" if tox_required else tox_status,
            "tox_error": tox_error,
            "tox_duration_seconds": round(tox_duration, 3),
            "tox_source_document_id": tox_source_document_id,
            "tox_study_id": tox_study_id,
        }
    finally:
        if repo is not None:
            repo.close()


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.bucket:
        print("S3 bucket is required (set --bucket or S3_BUCKET).", file=sys.stderr)
        return 1

    if not args.prefix and not args.project:
        print("Project is required unless --prefix is supplied.", file=sys.stderr)
        return 1

    if args.mode in {"auto", "core"}:
        if not args.tenant_id:
            print("tenant_id is required (set --tenant-id or DEFAULT_TENANT_ID).", file=sys.stderr)
            return 1
        if not args.created_by:
            print("created_by is required (set --created-by or DEFAULT_USER_ID).", file=sys.stderr)
            return 1

    if args.mode in {"auto", "tox"} and not args.project_id:
        print("project_id is required for tox pipeline (--project-id or PROJECT_ID).", file=sys.stderr)
        return 1

    prefix = args.prefix or f"{args.company.rstrip('/')}/{args.project.strip('/')}/"
    if not prefix.endswith("/"):
        prefix = f"{prefix}/"

    if args.core_text_engines:
        os.environ["PDF_PIPELINE_TEXT_ENGINES"] = args.core_text_engines
    if args.disable_core_tables:
        os.environ["PDF_PIPELINE_DISABLE_TABLES"] = "1"
    elif args.core_table_engines:
        os.environ["PDF_PIPELINE_TABLE_ENGINES"] = args.core_table_engines
    else:
        # Safe default to avoid camelot/tabula crashes; override with --core-table-engines.
        os.environ.setdefault("PDF_PIPELINE_TABLE_ENGINES", "pdfplumber")

    client = boto3.client("s3", region_name=args.aws_region)

    run_id = f"module{args.module}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    report_dir = Path(args.report_dir).expanduser()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json_path = report_dir / f"ingestion_{run_id}.json"
    report_csv_path = report_dir / f"ingestion_{run_id}.csv"

    started_at = datetime.now(timezone.utc)
    entries: list[Dict[str, Any]] = []
    doc_processed = 0
    doc_failed = 0
    doc_skipped = 0
    core_runs = 0
    core_failed = 0
    langchain_runs = 0
    langchain_failed = 0
    tox_runs = 0
    tox_failed = 0
    error_counts: Dict[str, int] = {}

    docs: list[Dict[str, Any]] = []
    for obj in _iter_module_pdfs(client, args.bucket, prefix, args.module):
        if args.limit is not None and len(docs) >= args.limit:
            break
        docs.append(obj)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(_process_document, obj, args) for obj in docs]
        for future in as_completed(futures):
            entry = future.result()
            entries.append(entry)

            required = (
                entry.get("core_required")
                or entry.get("langchain_required")
                or entry.get("tox_required")
            )
            failed = (
                (entry.get("core_required") and entry.get("core_status") == "failed")
                or (entry.get("langchain_required") and entry.get("langchain_status") == "failed")
                or (entry.get("tox_required") and entry.get("tox_status") == "failed")
            )

            if entry.get("core_required"):
                core_runs += 1
                if entry.get("core_status") == "failed":
                    core_failed += 1
            if entry.get("langchain_required"):
                langchain_runs += 1
                if entry.get("langchain_status") == "failed":
                    langchain_failed += 1
            if entry.get("tox_required"):
                tox_runs += 1
                if entry.get("tox_status") == "failed":
                    tox_failed += 1

            if failed:
                doc_failed += 1
            elif required:
                doc_processed += 1
            else:
                doc_skipped += 1

            for err in (entry.get("core_error"), entry.get("langchain_error"), entry.get("tox_error")):
                if err:
                    name = err.split(":", 1)[0]
                    error_counts[name] = error_counts.get(name, 0) + 1

            status = "skipped"
            if failed:
                status = "failed"
            elif required:
                status = "completed"
            print(f"[{status.upper()}] {entry['key']}")
            if failed:
                if entry.get("core_error"):
                    print(f"  core_error: {entry['core_error']}", file=sys.stderr)
                if entry.get("langchain_error"):
                    print(f"  langchain_error: {entry['langchain_error']}", file=sys.stderr)
                if entry.get("tox_error"):
                    print(f"  tox_error: {entry['tox_error']}", file=sys.stderr)

    ended_at = datetime.now(timezone.utc)
    report = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "bucket": args.bucket,
        "company": args.company,
        "project": args.project,
        "project_id": args.project_id,
        "module": args.module,
        "prefix": prefix,
        "mode": args.mode,
        "dry_run": args.dry_run,
        "core_check": args.core_check,
        "md_suffix": args.md_suffix,
        "force_core": bool(args.force or args.force_core),
        "force_langchain": args.force_langchain,
        "force_tox": args.force_tox,
        "workers": args.workers,
        "doc_processed": doc_processed,
        "doc_failed": doc_failed,
        "doc_skipped": doc_skipped,
        "core_runs": core_runs,
        "core_failed": core_failed,
        "langchain_runs": langchain_runs,
        "langchain_failed": langchain_failed,
        "tox_runs": tox_runs,
        "tox_failed": tox_failed,
        "error_summary": error_counts,
        "entries": entries,
    }

    with report_json_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    with report_csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "key",
                "version_id",
                "last_modified",
                "md_exists",
                "status_completed",
                "tox_data_exists",
                "core_required",
                "core_status",
                "core_error",
                "core_duration_seconds",
                "langchain_required",
                "langchain_status",
                "langchain_error",
                "langchain_duration_seconds",
                "document_version_id",
                "tox_required",
                "tox_status",
                "tox_error",
                "tox_duration_seconds",
                "tox_source_document_id",
                "tox_study_id",
            ],
        )
        writer.writeheader()
        writer.writerows(entries)

    try:
        repo = NCDRepository()
        repo.create_ingestion_run_report(
            run_id=run_id,
            bucket=args.bucket,
            company=args.company,
            project=args.project,
            project_id=args.project_id,
            module=args.module,
            mode=args.mode,
            core_check=args.core_check,
            md_suffix=args.md_suffix,
            force_core=bool(args.force or args.force_core),
            force_tox=args.force_tox,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            doc_processed=doc_processed,
            doc_failed=doc_failed,
            doc_skipped=doc_skipped,
            core_runs=core_runs,
            core_failed=core_failed,
            tox_runs=tox_runs,
            tox_failed=tox_failed,
            error_summary=error_counts,
            report_json=report,
        )
    except Exception as exc:
        print(f"[WARN] Unable to persist run report to DB: {exc}", file=sys.stderr)

    print(
        "\nCompleted. processed=%d failed=%d skipped=%d core_runs=%d langchain_runs=%d tox_runs=%d"
        % (doc_processed, doc_failed, doc_skipped, core_runs, langchain_runs, tox_runs)
    )
    print(f"Report JSON: {report_json_path}")
    print(f"Report CSV:  {report_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
