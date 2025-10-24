# author: Bin Lee
# email: blee@filynai.com

from __future__ import annotations

import contextlib
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Sequence

from pdf_analysis.pipeline import PDFProcessingPipeline, PipelineConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class S3Document:
    key: str
    company: str
    project: str
    module_label: str
    module_number: Optional[int]
    version_id: str
    last_modified: datetime

    @property
    def filename(self) -> str:
        return Path(self.key).name

    @property
    def module_slug(self) -> str:
        return _slugify(self.module_label)

    @property
    def project_slug(self) -> str:
        return _slugify(self.project)


@dataclass(slots=True)
class S3SyncConfig:
    bucket: str
    company: str = "filynai.com"
    projects: Sequence[str] | None = None
    module_filters: Sequence[int] | None = None
    aws_region: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_session_token: Optional[str] = None
    pipeline_config: PipelineConfig = field(default_factory=PipelineConfig)
    redis_url: Optional[str] = None
    redis_client: Any | None = None
    redis_factory: Callable[[], Any] | None = None
    redis_key_template: str = "{company}:{project_slug}:{module_slug}:{filename}"
    redis_expire_seconds: Optional[int] = None
    maximum_documents: Optional[int] = None


class S3RedisSyncService:
    """
    Periodically scans an S3 bucket for PDF documents and persists
    processed markdown into Redis keyed by folder structure.
    """

    def __init__(
        self,
        config: S3SyncConfig,
        *,
        pipeline_factory: Optional[Callable[[PipelineConfig], PDFProcessingPipeline]] = None,
    ) -> None:
        self.config = config
        self._pipeline_factory = pipeline_factory or self._default_pipeline_factory
        self._redis_client: Any | None = None

    # ------------------------------------------------------------------
    def run(self) -> int:
        """
        Execute a full scan. Returns the number of documents processed.
        """
        s3_client = self._build_s3_client()
        redis_client = self._resolve_redis_client()
        pipeline = self._pipeline_factory(self.config.pipeline_config)
        processed = 0

        for document in self._iter_latest_documents(s3_client):
            if self.config.maximum_documents and processed >= self.config.maximum_documents:
                break

            try:
                markdown = self._process_document(pipeline, s3_client, document)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to process %s@%s: %s", document.key, document.version_id, exc)
                continue

            if not markdown:
                logger.debug("Skipping %s (no markdown produced)", document.key)
                continue

            redis_key = self.config.redis_key_template.format(
                company=document.company,
                project=document.project,
                project_slug=document.project_slug,
                module=document.module_label,
                module_slug=document.module_slug,
                module_number=document.module_number if document.module_number is not None else "",
                filename=document.filename,
            )
            payload = {
                "s3_bucket": self.config.bucket,
                "s3_key": document.key,
                "s3_version": document.version_id,
                "last_modified": document.last_modified.astimezone(timezone.utc).isoformat(),
                "company": document.company,
                "project": document.project,
                "module_label": document.module_label,
                "module_number": document.module_number,
                "module_slug": document.module_slug,
                "project_slug": document.project_slug,
                "filename": document.filename,
                "markdown": markdown,
            }
            redis_client.hset(redis_key, mapping=payload)
            if self.config.redis_expire_seconds:
                redis_client.expire(redis_key, int(self.config.redis_expire_seconds))
            logger.debug("Persisted markdown to redis key=%s version=%s", redis_key, document.version_id)
            processed += 1

        logger.info("S3 sync completed, processed %d document(s)", processed)
        return processed

    # ------------------------------------------------------------------
    def _process_document(
        self,
        pipeline: PDFProcessingPipeline,
        s3_client: Any,
        document: S3Document,
    ) -> Optional[str]:
        with self._download_to_tempfile(s3_client, document) as temp_path:
            result = pipeline.run(temp_path)
        return result.markdown

    @contextlib.contextmanager
    def _download_to_tempfile(self, s3_client: Any, document: S3Document) -> Iterator[Path]:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        try:
            with tmp:
                extra: Dict[str, Any] = {"VersionId": document.version_id} if document.version_id else {}
                s3_client.download_fileobj(self.config.bucket, document.key, tmp, ExtraArgs=extra)
                tmp.flush()
            yield Path(tmp.name)
        finally:
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("Unable to remove temporary file %s", tmp.name, exc_info=True)

    # ------------------------------------------------------------------
    def _iter_latest_documents(self, s3_client: Any) -> Iterable[S3Document]:
        prefix = f"{self.config.company.rstrip('/')}/"
        paginator = s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            for version in page.get("Versions", []):
                if not version.get("IsLatest"):
                    continue
                key: str = version["Key"]
                if not key.lower().endswith(".pdf"):
                    continue
                try:
                    parsed = self._extract_document_metadata(key)
                except ValueError:
                    logger.debug("Skipping key outside expected structure: %s", key)
                    continue

                if self.config.projects and parsed.project not in self.config.projects:
                    continue
                if (
                    self.config.module_filters
                    and parsed.module_number is not None
                    and parsed.module_number not in self.config.module_filters
                ):
                    continue

                last_modified = version.get("LastModified")
                if isinstance(last_modified, datetime):
                    lm = last_modified
                else:
                    lm = datetime.now(timezone.utc)

                yield S3Document(
                    key=key,
                    company=parsed.company,
                    project=parsed.project,
                    module_label=parsed.module_label,
                    module_number=parsed.module_number,
                    version_id=version.get("VersionId", ""),
                    last_modified=lm,
                )

    def _extract_document_metadata(self, key: str) -> "ParsedKey":
        parts = key.split("/")
        if len(parts) < 4:
            raise ValueError("Missing project/module segments")
        company = parts[0]
        project = parts[1]
        module_segment = parts[2]

        match = _MODULE_PATTERN.match(module_segment)
        if not match:
            raise ValueError("Module segment not recognised")
        number_str = match.group("number")
        module_number = int(number_str) if number_str is not None else None

        return ParsedKey(
            company=company,
            project=project,
            module_label=module_segment,
            module_number=module_number,
        )

    # ------------------------------------------------------------------
    def _build_s3_client(self) -> Any:
        try:
            import boto3
        except ModuleNotFoundError as exc:  # pragma: no cover - user environment issue
            raise RuntimeError(
                "boto3 is required for S3 synchronization. Install it with `poetry add boto3` or "
                "`poetry install --with cloud_ocr,infra`."
            ) from exc

        session_kwargs: Dict[str, Any] = {}
        if self.config.aws_access_key_id:
            session_kwargs["aws_access_key_id"] = self.config.aws_access_key_id
        if self.config.aws_secret_access_key:
            session_kwargs["aws_secret_access_key"] = self.config.aws_secret_access_key
        if self.config.aws_session_token:
            session_kwargs["aws_session_token"] = self.config.aws_session_token
        if self.config.aws_region:
            session_kwargs["region_name"] = self.config.aws_region

        logger.debug("Creating S3 client for bucket=%s region=%s", self.config.bucket, self.config.aws_region)
        return boto3.client("s3", **session_kwargs)

    def _resolve_redis_client(self) -> Any:
        if self._redis_client is not None:
            return self._redis_client
        if self.config.redis_client is not None:
            self._redis_client = self.config.redis_client
            return self._redis_client
        if self.config.redis_factory is not None:
            self._redis_client = self.config.redis_factory()
            return self._redis_client
        if not self.config.redis_url:
            raise RuntimeError("Redis configuration missing: provide redis_client, redis_factory, or redis_url.")

        try:
            import redis
        except ModuleNotFoundError as exc:  # pragma: no cover - user environment issue
            raise RuntimeError(
                "redis-py is required. Install it with `poetry add redis` or `poetry install --with infra`."
            ) from exc

        self._redis_client = redis.Redis.from_url(self.config.redis_url)
        return self._redis_client

    # ------------------------------------------------------------------
    @staticmethod
    def _default_pipeline_factory(config: PipelineConfig) -> PDFProcessingPipeline:
        return PDFProcessingPipeline(config=config)


_MODULE_PATTERN = re.compile(r"^module\s*(?P<number>\d+)(?:[\s._-].*)?$", re.IGNORECASE)


def _slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "unknown"


@dataclass(slots=True)
class ParsedKey:
    company: str
    project: str
    module_label: str
    module_number: Optional[int]
