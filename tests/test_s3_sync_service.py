# author: Bin Lee
# email: blee@filynai.com

from __future__ import annotations

import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List
from unittest.mock import patch

from pdf_analysis.pipeline import PipelineConfig
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
            versions = [v for v in page.get("Versions", []) if v.get("Key", "").startswith(prefix)]
            if versions:
                yield {"Versions": versions}


class FakeS3Client:
    def __init__(self) -> None:
        self.downloads: List[Path] = []
        self._paginator = FakePaginator(
            [
                {
                    "Versions": [
                        {
                            "Key": "filynai.com/LT1009/Module 1.Quality/report.pdf",
                            "VersionId": "abc123",
                            "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                            "IsLatest": True,
                        },
                        {
                            "Key": "filynai.com/LT1009/Module 1.Quality/old_report.pdf",
                            "VersionId": "old1",
                            "LastModified": datetime(2023, 1, 1, tzinfo=timezone.utc),
                            "IsLatest": False,
                        },
                        {
                            "Key": "filynai.com/LT1009/Module 3.Safety/readme.txt",
                            "VersionId": "notpdf",
                            "LastModified": datetime(2024, 3, 1, tzinfo=timezone.utc),
                            "IsLatest": True,
                        },
                        {
                            "Key": "filynai.com/LT2001/Module 2.CMC/spec.pdf",
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


class DummyPipeline:
    def run(self, _: Path) -> types.SimpleNamespace:
        return types.SimpleNamespace(markdown="# mock\n\ncontent")


class S3RedisSyncServiceTests(unittest.TestCase):
    def test_run_writes_markdown_to_redis(self) -> None:
        fake_redis = FakeRedis()
        config = S3SyncConfig(
            bucket="demo-bucket",
            company="filynai.com",
            projects=("LT1009",),
            module_filters=(1,),
            redis_client=fake_redis,
            pipeline_config=PipelineConfig(),
        )
        service = S3RedisSyncService(
            config,
            pipeline_factory=lambda _: DummyPipeline(),  # type: ignore[arg-type]
        )

        fake_s3 = FakeS3Client()
        with patch.object(S3RedisSyncService, "_build_s3_client", return_value=fake_s3):
            processed = service.run()

        self.assertEqual(processed, 1)
        key = "filynai.com:lt1009:module-1-quality:report.pdf"
        self.assertIn(key, fake_redis.store)
        payload = fake_redis.store[key]
        self.assertEqual(payload["s3_bucket"], "demo-bucket")
        self.assertEqual(payload["s3_version"], "abc123")
        self.assertTrue(payload["markdown"].startswith("# mock"))
        self.assertEqual(payload["project"], "LT1009")
        self.assertEqual(payload["module_number"], 1)


if __name__ == "__main__":
    unittest.main()
