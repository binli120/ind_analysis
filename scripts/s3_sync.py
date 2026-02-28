# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""CLI helper that mirrors S3 extracts into Redis (and optional AI metadata)."""

# author: Bin Lee
# email: blee@longooc.com

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from pdf_analysis.pipeline import PipelineConfig
from pdf_analysis.service.s3_sync import S3RedisSyncService, S3SyncConfig

try:  # optional dependency loaded via `poetry install --with infra`
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - handled gracefully at runtime
    load_dotenv = None


def _load_env_files() -> None:
    """Read optional dotenv files so users can store AWS/Redis credentials locally."""
    env_paths = [Path(".env"), Path(".env.local")]
    existing = [path for path in env_paths if path.is_file()]
    if not existing:
        return
    if load_dotenv is None:
        logging.getLogger(__name__).warning(
            "python-dotenv is not installed; skipping env files: %s",
            ", ".join(str(path) for path in existing),
        )
        return
    for path in existing:
        load_dotenv(path, override=True)


_load_env_files()


def _parse_modules(raw: str | None) -> Sequence[int]:
    """Convert a comma-separated module list into integers."""
    if not raw:
        return ()
    items = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            number = int(entry)
        except ValueError as exc:
            raise SystemExit(
                f"Invalid module number '{entry}'. Expected integers."
            ) from exc
        items.append(number)
    return tuple(items)


def _parse_projects(raw: str | None) -> Sequence[str]:
    """Convert a comma-separated project list into clean strings."""
    if not raw:
        return ()
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Build and parse the CLI arguments for the sync utility."""
    parser = argparse.ArgumentParser(
        prog="s3-sync",
        description="Synchronise PDF markdown extracts from S3 into Redis.",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("S3_BUCKET"),
        help="S3 bucket containing company folders (defaults to S3_BUCKET env).",
    )
    parser.add_argument(
        "--company",
        default="longooc.com",
        help="Top-level company folder (default: longooc.com).",
    )
    parser.add_argument(
        "--modules",
        help="Comma-separated list of module numbers to include (e.g. 1,2,3). Defaults to all.",
    )
    parser.add_argument(
        "--projects",
        help="Comma-separated list of project folders to include (default: all projects).",
    )
    parser.add_argument(
        "--aws-region",
        default=os.getenv("AWS_REGION"),
        help="AWS region override (defaults to AWS_REGION env).",
    )
    parser.add_argument(
        "--aws-access-key-id",
        default=os.getenv("AWS_ACCESS_KEY_ID"),
        help="AWS access key (defaults to env).",
    )
    parser.add_argument(
        "--aws-secret-access-key",
        default=os.getenv("AWS_SECRET_ACCESS_KEY"),
        help="AWS secret key (defaults to env).",
    )
    parser.add_argument(
        "--aws-session-token",
        default=os.getenv("AWS_SESSION_TOKEN"),
        help="AWS session token if using temporary credentials.",
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL"),
        help="Redis connection URL (e.g. redis://localhost:6379/0).",
    )
    parser.add_argument(
        "--redis-key-template",
        default="{company}:{project_slug}:{module_slug}:{filename}",
        help="Template for Redis keys.",
    )
    parser.add_argument(
        "--redis-expire",
        type=int,
        default=None,
        help="Optional TTL (seconds) to set on redis hashes.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory where extracted markdown files will be written (mirrors S3 hierarchy).",
    )
    parser.add_argument(
        "--ai-metadata",
        action="store_true",
        help="Enable OpenAI-powered metadata labeling (requires OPENAI_API_KEY and infra extras).",
    )
    parser.add_argument(
        "--ai-model",
        default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        help="OpenAI model to use when --ai-metadata is enabled (default: gpt-4o-mini).",
    )
    parser.add_argument(
        "--ai-embeddings",
        action="store_true",
        help="Generate OpenAI embeddings and upsert them into Supabase (requires infra extras).",
    )
    parser.add_argument(
        "--ai-summary",
        action="store_true",
        help="Generate document summaries and topic anchors via OpenAI.",
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        help="Embedding model to use when --ai-embeddings is enabled (default: text-embedding-3-small).",
    )
    parser.add_argument(
        "--supabase-on-conflict",
        default=os.getenv("SUPABASE_ON_CONFLICT"),
        help="Column(s) used for Supabase upsert conflict resolution (e.g., filename). Leave blank to always insert.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of documents to process in this run.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess documents even if markdown/meta sidecars already exist.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, ...).",
    )
    parser.add_argument(
        "--summary-model",
        default=os.getenv("OPENAI_SUMMARY_MODEL", "gpt-4o-mini"),
        help="OpenAI model to use when --ai-summary is enabled (default: gpt-4o-mini).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """Entrypoint for syncing documents from S3 into Redis and optional backends."""
    args = parse_args(argv)
    if not args.bucket:
        raise SystemExit(
            "S3 bucket is required. Set --bucket or S3_BUCKET in environment/.env.local."
        )
    logging.basicConfig(level=args.log_level.upper())

    modules = _parse_modules(args.modules)
    projects = _parse_projects(args.projects)
    config = S3SyncConfig(
        bucket=args.bucket,
        company=args.company,
        projects=projects or None,
        module_filters=modules or None,
        aws_region=args.aws_region,
        aws_access_key_id=args.aws_access_key_id,
        aws_secret_access_key=args.aws_secret_access_key,
        aws_session_token=args.aws_session_token,
        pipeline_config=PipelineConfig(),
        redis_url=args.redis_url,
        redis_key_template=args.redis_key_template,
        redis_expire_seconds=args.redis_expire,
        maximum_documents=args.limit,
        output_dir=Path(args.output_dir).resolve() if args.output_dir else None,
        force=args.force,
    )

    enable_ai = (
        args.ai_metadata or os.getenv("ENABLE_AI_METADATA", "false").lower() == "true"
    )
    if not enable_ai and os.getenv("ENABLE_AI_META_DATA", "false").lower() == "true":
        enable_ai = True

    metadata_generator = None
    if enable_ai:
        try:
            from pdf_analysis.service.ai_metadata import OpenAIMetadataGenerator

            metadata_generator = OpenAIMetadataGenerator(model=args.ai_model)
        except ModuleNotFoundError:
            logging.warning(
                "OpenAI metadata generator unavailable. Install infra extras to enable it."
            )

    enable_embeddings = (
        args.ai_embeddings
        or os.getenv("ENABLE_AI_EMBEDDINGS", "false").lower() == "true"
    )

    embedding_store = None
    if enable_embeddings:
        try:
            from pdf_analysis.service.embedding_store import SupabaseEmbeddingStore

            candidate_store = SupabaseEmbeddingStore(
                embedding_model=args.embedding_model,
                on_conflict=args.supabase_on_conflict,
            )
            if candidate_store.is_available():
                embedding_store = candidate_store
            else:
                logging.warning(
                    "Supabase embedding store not fully configured; embeddings will be skipped."
                )
        except ModuleNotFoundError:
            logging.warning(
                "Supabase embedding store unavailable. Install infra extras to enable it."
            )

    enable_summary = (
        args.ai_summary or os.getenv("ENABLE_AI_SUMMARY", "false").lower() == "true"
    )
    summary_generator = None
    if enable_summary:
        try:
            from pdf_analysis.service.document_summarizer import (
                OpenAIDocumentSummarizer,
            )

            summary_generator = OpenAIDocumentSummarizer(model=args.summary_model)
            if not summary_generator.is_available():
                logging.warning(
                    "Document summarizer is not available; summary generation skipped."
                )
                summary_generator = None
        except ModuleNotFoundError:
            logging.warning(
                "Document summarizer unavailable. Install infra extras to enable it."
            )

    service = S3RedisSyncService(
        config,
        metadata_generator=metadata_generator,
        embedding_store=embedding_store,
        summarizer=summary_generator,
    )
    processed = service.run()
    logging.info("Processed %d documents.", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
