# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Tests for S3 to Redis sync service and its supporting utilities."""

# author: Bin Lee
# email: blee@longooc.com

from __future__ import annotations

import io
import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from unittest.mock import patch

from pdf_analysis.pipeline import PipelineConfig
from pdf_analysis.service.document_summarizer import TopicSection, TopicSummaryResult
from pdf_analysis.service.s3_sync import S3RedisSyncService, S3SyncConfig


class FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, Dict[str, str]] = {}
        self.ttl: Dict[str, int] = {}

    def hset(self, key: str, mapping: Dict[str, str]) -> None:
        self.store.setdefault(key, {}).update(mapping)

    def expire(self, key: str, seconds: int) -> None:
        self.ttl[key] = seconds


class FakePaginator:
    def __init__(self, pages: List[Dict[str, object]]) -> None:
        self._pages = pages

    def paginate(self, **kwargs: object) -> Iterable[Dict[str, object]]:
        prefix = kwargs.get("Prefix", "")
        for page in self._pages:
            versions = [
                v
                for v in page.get("Versions", [])
                if v.get("Key", "").startswith(prefix)
            ]
            if versions:
                yield {"Versions": versions}


class FakeS3Client:
    def __init__(self) -> None:
        self.downloads: List[Path] = []
        self.copies: List[Dict[str, Any]] = []
        self.puts: List[Dict[str, Any]] = []
        self.metadata_store: Dict[tuple[str, str], Dict[str, str]] = {}
        self.object_store: Dict[tuple[str, str], bytes] = {}
        self._paginator = FakePaginator(
            [
                {
                    "Versions": [
                        {
                            "Key": "longooc.com/LT1009/Module 1.Quality/report.pdf",
                            "VersionId": "abc123",
                            "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                            "IsLatest": True,
                        },
                        {
                            "Key": "longooc.com/LT1009/Module 1.Quality/old_report.pdf",
                            "VersionId": "old1",
                            "LastModified": datetime(2023, 1, 1, tzinfo=timezone.utc),
                            "IsLatest": False,
                        },
                        {
                            "Key": "longooc.com/LT1009/Module 3.Safety/readme.txt",
                            "VersionId": "notpdf",
                            "LastModified": datetime(2024, 3, 1, tzinfo=timezone.utc),
                            "IsLatest": True,
                        },
                        {
                            "Key": "longooc.com/LT2001/Module 2.CMC/spec.pdf",
                            "VersionId": "def456",
                            "LastModified": datetime(2024, 2, 10, tzinfo=timezone.utc),
                            "IsLatest": True,
                        },
                    ]
                }
            ]
        )

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_object_versions"
        return self._paginator

    def download_fileobj(self, bucket: str, key: str, fileobj, ExtraArgs=None) -> None:
        _ = (bucket, key, ExtraArgs)
        fileobj.write(b"%PDF-1.4 mock")

    def head_object(
        self, Bucket: str, Key: str, VersionId: str | None = None
    ) -> Dict[str, Any]:  # type: ignore[override]
        _ = VersionId
        if (Bucket, Key) in self.object_store:
            return {
                "Metadata": self.metadata_store.get((Bucket, Key), {}),
                "ContentType": "text/plain",
            }
        if Key.endswith(".pdf"):
            return {
                "Metadata": self.metadata_store.get((Bucket, Key), {}),
                "ContentType": "application/pdf",
            }
        raise KeyError(f"{Key} not found")

    def copy_object(self, **kwargs: Any) -> Dict[str, Any]:
        self.copies.append(kwargs)
        meta = kwargs.get("Metadata", {})
        self.metadata_store[(kwargs["Bucket"], kwargs["Key"])] = meta
        return {"CopyObjectResult": {}}

    def put_object(self, **kwargs: Any) -> Dict[str, Any]:
        self.puts.append(kwargs)
        body = kwargs.get("Body", b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.object_store[(kwargs["Bucket"], kwargs["Key"])] = body
        return {"VersionId": "meta-version"}

    def get_object(self, Bucket: str, Key: str) -> Dict[str, Any]:
        try:
            body = self.object_store[(Bucket, Key)]
        except KeyError as exc:
            raise KeyError(f"{Key} not found") from exc
        return {"Body": io.BytesIO(body)}


class FakeEmbeddingStore:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def store_document(self, **payload: Any) -> bool:
        self.calls.append(payload)
        return True


class DummyPipeline:
    def run(self, _: Path) -> types.SimpleNamespace:
        return types.SimpleNamespace(markdown="# mock\n\ncontent")


class S3RedisSyncServiceTests(unittest.TestCase):
    def test_run_writes_markdown_to_redis(self) -> None:
        fake_redis = FakeRedis()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir)
            config = S3SyncConfig(
                bucket="demo-bucket",
                company="longooc.com",
                projects=("LT1009",),
                module_filters=(1,),
                redis_client=fake_redis,
                pipeline_config=PipelineConfig(),
                output_dir=output_path,
            )

            def metadata_stub(_: str) -> Dict[str, Any]:
                return {
                    "labels": ["clinical", "efficacy"],
                    "keywords": ["efficacy", "safety"],
                    "language": "en",
                }

            class FakeSummarizer:
                def __init__(self) -> None:
                    self.calls = 0

                def __call__(self, _: str) -> TopicSummaryResult:
                    self.calls += 1
                    return TopicSummaryResult(
                        summary="Overall summary text.",
                        topics=[
                            TopicSection(
                                title="Clinical Outcomes",
                                description="Efficacy measurements and endpoints.",
                                anchor="clinical-outcomes",
                            ),
                            TopicSection(
                                title="Safety Monitoring",
                                description="Adverse events and monitoring plans.",
                                anchor="safety-monitoring",
                            ),
                        ],
                    )

            fake_embedding_store = FakeEmbeddingStore()
            fake_summarizer = FakeSummarizer()
            service = S3RedisSyncService(
                config,
                pipeline_factory=lambda _: DummyPipeline(),  # type: ignore[arg-type]
                metadata_generator=metadata_stub,
                embedding_store=fake_embedding_store,
                summarizer=fake_summarizer,
            )

            fake_s3 = FakeS3Client()
            with patch.object(
                S3RedisSyncService, "_build_s3_client", return_value=fake_s3
            ):
                processed = service.run()

            self.assertEqual(processed, 1)
            key = "longooc.com:lt1009:module-1-quality:report.pdf"
            self.assertIn(key, fake_redis.store)
            payload = fake_redis.store[key]
            self.assertEqual(payload["s3_bucket"], "demo-bucket")
            self.assertEqual(payload["s3_version"], "abc123")
            self.assertIn("# mock", payload["markdown"])
            self.assertIn("## Key Topics", payload["markdown"])
            self.assertEqual(payload["project"], "LT1009")
            self.assertEqual(payload["module_number"], "1")
            self.assertEqual(payload["labels"], "clinical,efficacy")
            self.assertEqual(payload["keywords"], "efficacy,safety")
            metadata_json = json.loads(payload["metadata_json"])
            self.assertTrue(metadata_json["analyzed"])
            self.assertIn("summary_text", payload)

            markdown_path = (
                output_path
                / "longooc.com"
                / "LT1009"
                / "Module 1.Quality"
                / "report.abc123.pdf.md"
            )
            meta_path = (
                output_path
                / "longooc.com"
                / "LT1009"
                / "Module 1.Quality"
                / "report.abc123.pdf.meta.json"
            )
            summary_path = (
                output_path
                / "longooc.com"
                / "LT1009"
                / "Module 1.Quality"
                / "report.abc123.pdf.summary.txt"
            )
            self.assertTrue(markdown_path.exists())
            self.assertTrue(meta_path.exists())
            self.assertTrue(summary_path.exists())
            markdown_contents = markdown_path.read_text(encoding="utf-8")
            self.assertIn("# mock", markdown_contents)
            self.assertIn("## Key Topics", markdown_contents)
            meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(
                meta_payload["metadata"]["labels"], ["clinical", "efficacy"]
            )
            self.assertEqual(
                meta_payload["markdown_file"],
                "longooc.com/LT1009/Module 1.Quality/report.abc123.pdf.md",
            )
            self.assertEqual(
                meta_payload["markdown_key"],
                "longooc.com/LT1009/Module 1.Quality/report.pdf.md",
            )
            self.assertEqual(
                meta_payload["summary_file"],
                "longooc.com/LT1009/Module 1.Quality/report.abc123.pdf.summary.txt",
            )
            self.assertEqual(
                meta_payload["summary_key"],
                "longooc.com/LT1009/Module 1.Quality/report.pdf.summary.txt",
            )
            self.assertNotIn("markdown", meta_payload["redis"])
            self.assertIn("Key Topics", summary_path.read_text(encoding="utf-8"))

            self.assertEqual(len(fake_s3.copies), 1)
            self.assertIn("Metadata", fake_s3.copies[0])
            self.assertEqual(
                fake_s3.copies[0]["Metadata"]["labels"], "clinical,efficacy"
            )
            self.assertEqual(len(fake_s3.puts), 3)
            self.assertTrue(
                any(entry["Key"].endswith("report.pdf.md") for entry in fake_s3.puts)
            )
            self.assertTrue(
                any(
                    entry["Key"].endswith("report.pdf.meta.json")
                    for entry in fake_s3.puts
                )
            )
            self.assertTrue(
                any(
                    entry["Key"].endswith("report.pdf.summary.txt")
                    for entry in fake_s3.puts
                )
            )
            self.assertEqual(len(fake_embedding_store.calls), 1)
            self.assertEqual(
                fake_embedding_store.calls[0]["s3_key"],
                "longooc.com/LT1009/Module 1.Quality/report.pdf",
            )
            self.assertEqual(fake_summarizer.calls, 1)

    def test_skips_documents_when_sidecars_exist(self) -> None:
        fake_redis = FakeRedis()
        config = S3SyncConfig(
            bucket="demo-bucket",
            company="longooc.com",
            projects=("LT1009",),
            module_filters=(1,),
            redis_client=fake_redis,
            pipeline_config=PipelineConfig(),
        )

        class CountingPipeline:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, _: Path) -> types.SimpleNamespace:
                self.calls += 1
                return types.SimpleNamespace(markdown="# mock\n\ncontent")

        pipeline = CountingPipeline()
        service = S3RedisSyncService(
            config,
            pipeline_factory=lambda _: pipeline,  # type: ignore[arg-type]
        )

        fake_s3 = FakeS3Client()
        meta_key = "longooc.com/LT1009/Module 1.Quality/report.pdf.meta.json"
        md_key = "longooc.com/LT1009/Module 1.Quality/report.pdf.md"
        existing_meta = {
            "bucket": "demo-bucket",
            "key": "longooc.com/LT1009/Module 1.Quality/report.pdf",
            "version_id": "abc123",
            "metadata": {"analyzed": True},
            "markdown_key": md_key,
        }
        fake_s3.put_object(
            Bucket="demo-bucket", Key=meta_key, Body=json.dumps(existing_meta)
        )
        fake_s3.put_object(Bucket="demo-bucket", Key=md_key, Body=b"# existing\n")

        with patch.object(S3RedisSyncService, "_build_s3_client", return_value=fake_s3):
            processed = service.run()

        self.assertEqual(processed, 0)
        self.assertEqual(pipeline.calls, 0)
        self.assertEqual(fake_redis.store, {})
        self.assertEqual(len(fake_s3.puts), 2)  # seeded objects only

    def test_force_reprocesses_even_when_sidecars_exist(self) -> None:
        fake_redis = FakeRedis()
        config = S3SyncConfig(
            bucket="demo-bucket",
            company="longooc.com",
            projects=("LT1009",),
            module_filters=(1,),
            redis_client=fake_redis,
            pipeline_config=PipelineConfig(),
            force=True,
        )

        class CountingPipeline:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, _: Path) -> types.SimpleNamespace:
                self.calls += 1
                return types.SimpleNamespace(markdown="# mock\n\ncontent")

        pipeline = CountingPipeline()
        service = S3RedisSyncService(
            config,
            pipeline_factory=lambda _: pipeline,  # type: ignore[arg-type]
        )

        fake_s3 = FakeS3Client()
        meta_key = "longooc.com/LT1009/Module 1.Quality/report.pdf.meta.json"
        md_key = "longooc.com/LT1009/Module 1.Quality/report.pdf.md"
        existing_meta = {
            "bucket": "demo-bucket",
            "key": "longooc.com/LT1009/Module 1.Quality/report.pdf",
            "version_id": "abc123",
            "metadata": {"analyzed": True},
            "markdown_key": md_key,
        }
        fake_s3.put_object(
            Bucket="demo-bucket", Key=meta_key, Body=json.dumps(existing_meta)
        )
        fake_s3.put_object(Bucket="demo-bucket", Key=md_key, Body=b"# existing\n")

        with patch.object(S3RedisSyncService, "_build_s3_client", return_value=fake_s3):
            processed = service.run()

        self.assertEqual(processed, 1)
        self.assertEqual(pipeline.calls, 1)
        self.assertIn(
            "longooc.com:lt1009:module-1-quality:report.pdf", fake_redis.store
        )
        self.assertGreater(len(fake_s3.puts), 2)  # new uploads appended


if __name__ == "__main__":
    unittest.main()
