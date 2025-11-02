# author: Bin Lee
# email: blee@filynai.com

from __future__ import annotations

import contextlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
    output_dir: Path | None = None


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
        metadata_generator: Optional[Callable[[str], Dict[str, Any]]] = None,
        embedding_store: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._pipeline_factory = pipeline_factory or self._default_pipeline_factory
        self._redis_client: Any | None = None
        self._metadata_generator = metadata_generator
        self._embedding_store = embedding_store

    # ------------------------------------------------------------------
    def run(self) -> int:
        """
        Execute a full scan. Returns the number of documents processed.
        """
        s3_client = self._build_s3_client()
        redis_client = self._resolve_redis_client()
        pipeline = self._pipeline_factory(self.config.pipeline_config)
        output_dir = Path(self.config.output_dir) if self.config.output_dir else None
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

            metadata_fields: Dict[str, Any] = {}
            if self._metadata_generator:
                try:
                    metadata_fields = self._metadata_generator(markdown) or {}
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Metadata generation failed for %s: %s", document.key, exc)
                    metadata_fields = {}
            metadata_fields.setdefault("analyzed", True)

            redis_key = self.config.redis_key_template.format(
                company=document.company,
                project=document.project,
                project_slug=document.project_slug,
                module=document.module_label,
                module_slug=document.module_slug,
                module_number=document.module_number if document.module_number is not None else "",
                filename=document.filename,
            )

            labels = metadata_fields.get("labels", [])
            keywords = metadata_fields.get("keywords", [])
            language = metadata_fields.get("language")

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
            if labels:
                payload["labels"] = ",".join(labels)
            if keywords:
                payload["keywords"] = ",".join(keywords)
            if language:
                payload["language"] = language
            payload["metadata_json"] = json.dumps(metadata_fields)

            redis_payload = {key: _stringify(value) for key, value in payload.items()}
            redis_client.hset(redis_key, mapping=redis_payload)
            if self.config.redis_expire_seconds:
                redis_client.expire(redis_key, int(self.config.redis_expire_seconds))
            logger.debug("Persisted markdown to redis key=%s version=%s", redis_key, document.version_id)

            redis_snapshot = dict(redis_payload)
            redis_snapshot.pop("markdown", None)

            meta_payload = {
                "bucket": self.config.bucket,
                "key": document.key,
                "version_id": document.version_id,
                "project": document.project,
                "module": document.module_label,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata_fields,
                "redis": redis_snapshot,
            }

            if metadata_fields:
                self._update_s3_metadata(s3_client, document, metadata_fields)
                self._upload_metadata_json(s3_client, document, meta_payload)

            if self._embedding_store:
                try:
                    self._embedding_store.store_document(
                        s3_bucket=self.config.bucket,
                        s3_key=document.key,
                        version_id=document.version_id,
                        filename=document.filename,
                        markdown=markdown,
                        metadata=metadata_fields,
                        company=document.company,
                        project=document.project,
                        module_label=document.module_label,
                        module_number=document.module_number,
                        labels=metadata_fields.get("labels"),
                        keywords=metadata_fields.get("keywords"),
                        language=metadata_fields.get("language"),
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Embedding storage failed for %s: %s", document.key, exc)

            if output_dir is not None:
                self._persist_markdown_file(output_dir, document, markdown, meta_payload)

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

    def _persist_markdown_file(
        self,
        base_dir: Path,
        document: S3Document,
        markdown: str,
        meta_payload: Dict[str, Any],
    ) -> None:
        relative = Path(document.key)
        parent = relative.parent
        safe_version = _safe_version(document.version_id) if document.version_id else None

        filename = relative.name
        if filename.lower().endswith(".pdf"):
            stem = filename[:-4]
            if safe_version:
                md_filename = f"{stem}.{safe_version}.pdf.md"
                meta_filename = f"{stem}.{safe_version}.pdf.meta.json"
            else:
                md_filename = f"{stem}.pdf.md"
                meta_filename = f"{stem}.pdf.meta.json"
        else:
            if safe_version:
                md_filename = f"{filename}.{safe_version}.md"
                meta_filename = f"{filename}.{safe_version}.meta.json"
            else:
                md_filename = f"{filename}.md"
                meta_filename = f"{filename}.meta.json"

        rel_md_path = (parent / md_filename) if str(parent) != "." else Path(md_filename)
        rel_meta_path = (parent / meta_filename) if str(parent) != "." else Path(meta_filename)

        md_path = base_dir / rel_md_path
        meta_path = base_dir / rel_meta_path

        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        meta_payload = dict(meta_payload)
        meta_payload["markdown_file"] = str(rel_md_path.as_posix())
        meta_path.write_text(json.dumps(meta_payload, indent=2), encoding="utf-8")
        logger.debug("Wrote markdown to %s and metadata to %s", md_path, meta_path)

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

    def _update_s3_metadata(
        self,
        s3_client: Any,
        document: S3Document,
        metadata_fields: Dict[str, Any],
    ) -> None:
        try:
            head_kwargs: Dict[str, Any] = {
                "Bucket": self.config.bucket,
                "Key": document.key,
            }
            if document.version_id:
                head_kwargs["VersionId"] = document.version_id
            head = s3_client.head_object(**head_kwargs)
            existing_metadata = head.get("Metadata", {})
            content_type = head.get("ContentType")
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Failed to read metadata for %s: %s", document.key, exc)
            existing_metadata = {}
            content_type = None

        new_metadata = dict(existing_metadata)
        labels = metadata_fields.get("labels")
        keywords = metadata_fields.get("keywords")
        language = metadata_fields.get("language")
        if labels:
            new_metadata["labels"] = ",".join(labels)
        if keywords:
            new_metadata["keywords"] = ",".join(keywords)
        if language:
            new_metadata["language"] = str(language)
        new_metadata["analyzed"] = "true"

        copy_source: Dict[str, Any] = {"Bucket": self.config.bucket, "Key": document.key}
        if document.version_id:
            copy_source["VersionId"] = document.version_id

        copy_kwargs: Dict[str, Any] = {
            "Bucket": self.config.bucket,
            "Key": document.key,
            "CopySource": copy_source,
            "MetadataDirective": "REPLACE",
            "TaggingDirective": "COPY",
            "Metadata": new_metadata,
        }
        if content_type:
            copy_kwargs["ContentType"] = content_type

        try:
            s3_client.copy_object(**copy_kwargs)
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Failed to persist metadata for %s: %s", document.key, exc)

    def _upload_metadata_json(
        self,
        s3_client: Any,
        document: S3Document,
        meta_payload: Dict[str, Any],
    ) -> None:
        meta_key = f"{document.key}.meta.json"
        try:
            s3_client.put_object(
                Bucket=self.config.bucket,
                Key=meta_key,
                Body=json.dumps(meta_payload, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Failed to upload metadata json for %s: %s", meta_key, exc)


_MODULE_PATTERN = re.compile(r"^module\s*(?P<number>\d+)(?:[\s._-].*)?$", re.IGNORECASE)


def _slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "unknown"


def _safe_version(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)


@dataclass(slots=True)
class ParsedKey:
    company: str
    project: str
    module_label: str
    module_number: Optional[int]


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value)
    return str(value)
