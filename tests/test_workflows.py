"""Integration-style tests for markdown writer, pipeline, and API workflows."""

# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, cast
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from pdf_analysis.api import server
from pdf_analysis.pipeline import (
    PDFProcessingPipeline,
    PipelineConfig,
    RedisStreamingConfig,
)
from pdf_analysis.service.document_summarizer import TopicSection, TopicSummaryResult
from pdf_analysis.transform.markdown_writer import build_markdown_document
from pdf_analysis.validate import generate_quality_report


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, object]] = {}
        self.ttl: Dict[str, int] = {}

    def hset(self, key: str, field: str, value: object) -> None:
        self.hashes.setdefault(key, {})[field] = value

    def expire(self, key: str, seconds: int) -> None:
        self.ttl[key] = seconds


class MarkdownWriterTests(unittest.TestCase):
    def test_build_markdown_document_includes_tables(self) -> None:
        manifest = [
            {
                "page_number": 1,
                "index_on_page": 1,
                "engine": "pdfplumber",
                "csv": "report.p1.t1.csv",
                "json": "report.p1.t1.json",
                "preview_rows": pd.DataFrame(
                    [("Site", "A"), ("Site", "B")],
                    columns=["Site", "Value"],
                ),
            }
        ]
        pages = [{"page_number": 1, "text": "Clinical study overview."}]

        markdown = build_markdown_document("report.pdf", pages, manifest)

        self.assertTrue(markdown.startswith("# report.pdf"))
        self.assertIn("Clinical study overview.", markdown)
        self.assertIn("**Table (p1 t1)**", markdown)
        self.assertIn("[CSV](report.p1.t1.csv)", markdown)
        self.assertIn("| Site | Value |", markdown)


class QualityReportTests(unittest.TestCase):
    def test_generate_quality_report_flags_inconsistencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "Report_ABC123.pdf"
            pages = [
                {"page_number": 1, "text": "Alpha"},
                {"page_number": 2, "text": "Beta"},
            ]
            dataframe = pd.DataFrame(
                [
                    ("Report Number", "XYZ-999"),
                    ("Number of Pages", "3"),
                    ("Initiation Date", "2020-02-01"),
                    ("Completion Date", "2020-01-01"),
                    ("Notes", None),
                ],
                columns=["col_1", "col_2"],
            )
            tables = [
                {
                    "page_number": 1,
                    "index_on_page": 1,
                    "engine": "pdfplumber",
                    "dataframe": dataframe,
                }
            ]

            report = generate_quality_report(pdf_path, pages, tables)

        issues = {issue["message"] for issue in report["json"]["issues"]}

        self.assertIn(
            "Report number 'XYZ-999' does not match file name 'Report_ABC123'.",
            issues,
        )
        self.assertIn("Table headers appear generic or auto-generated.", issues)
        self.assertIn("Detected 1 blank cell(s) in extracted table.", issues)
        self.assertIn(
            "Declared number of pages (3) does not match extracted page count (2).",
            issues,
        )
        self.assertIn("Completion Date occurs before Initiation Date.", issues)
        self.assertEqual(report["json"]["stats"]["tables_extracted"], 1)


class PipelineStreamingTests(unittest.TestCase):
    def test_stream_yields_chunks_and_writes_to_redis(self) -> None:
        fake_redis = FakeRedis()

        pipeline = PDFProcessingPipeline(
            PipelineConfig(
                redis=RedisStreamingConfig(
                    enabled=True,
                    client_factory=lambda _: fake_redis,
                    expire_seconds=30,
                    key_template="doc:{stem}",
                )
            )
        )

        fake_pages = [
            {"page_number": 1, "text": "Section overview"},
            {"page_number": 2, "text": ""},
        ]

        def fake_select_text_stream(self, pdf_path, extras):
            return iter(fake_pages), "pdfminer"

        def fake_tables_for_pages(self, pdf_path, page_numbers):
            tables = []
            engines = []
            if 2 in page_numbers:
                tables.append(
                    {
                        "page_number": 2,
                        "index_on_page": 1,
                        "engine": "pdfplumber",
                        "dataframe": None,
                    }
                )
                engines.append("pdfplumber")
            return tables, engines

        def fake_ocr(self, pdf_path, page_numbers):
            if 2 in page_numbers:
                return {2: "Recovered text via OCR"}, "tesseract"
            return {}, None

        with patch.object(
            PDFProcessingPipeline, "_select_text_stream", new=fake_select_text_stream
        ), patch.object(
            PDFProcessingPipeline,
            "_extract_tables_for_pages",
            new=fake_tables_for_pages,
        ), patch.object(
            PDFProcessingPipeline,
            "_run_ocr_for_page_numbers",
            new=fake_ocr,
        ):
            chunks = list(
                pipeline.stream(
                    Path("demo.pdf"),
                    chunk_size=1,
                )
            )

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].chunk_index, 1)
        self.assertEqual(chunks[0].pages[0]["text"], "Section overview")
        self.assertEqual(chunks[1].chunk_index, 2)
        self.assertEqual(chunks[1].pages[0]["text"], "Recovered text via OCR")
        self.assertEqual(chunks[1].ocr_pages, [2])
        self.assertEqual(chunks[1].table_engines, ("pdfplumber",))
        self.assertEqual(chunks[1].tables[0]["page_number"], 2)

        stored = fake_redis.hashes["doc:demo"]
        self.assertIn("chunk:00001", stored)
        chunk_two_payload = json.loads(cast(str, stored["chunk:00002"]))
        self.assertEqual(chunk_two_payload["ocr_pages"], [2])
        self.assertEqual(chunk_two_payload["tables"][0]["engine"], "pdfplumber")
        self.assertEqual(stored["status"], "complete")
        self.assertEqual(stored["chunks"], 2)
        self.assertEqual(stored["text_engine"], "pdfminer")
        self.assertEqual(fake_redis.ttl["doc:demo"], 30)


class ApiEndpointTests(unittest.TestCase):
    def test_analyze_pdf_endpoint_monkeypatched(self) -> None:
        fake_pages = [{"page_number": 1, "text": "Overview"}]
        fake_df = pd.DataFrame(
            [("Arm", "Control"), ("Arm", "Treatment")],
            columns=["Arm", "Group"],
        )
        fake_tables = [
            {
                "page_number": 1,
                "index_on_page": 1,
                "engine": "pdfplumber",
                "dataframe": fake_df,
            }
        ]

        with patch.object(server, "extract_pages_text", return_value=fake_pages), patch.object(
            server, "extract_tables_all", return_value=fake_tables
        ), patch.object(
            server,
            "generate_quality_report",
            return_value={
                "json": {
                    "document": "demo.pdf",
                    "stats": {
                        "document": "demo.pdf",
                        "pages_extracted": len(fake_pages),
                        "tables_extracted": len(fake_tables),
                    },
                    "issues": [],
                },
                "markdown": "# Quality",
            },
        ):
            client = TestClient(server.app)
            pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>\nendobj\ntrailer<<>>\n%%EOF"
            response = client.post(
                "/analyze",
                files={
                    "file": ("demo.pdf", io.BytesIO(pdf_bytes), "application/pdf"),
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["document"], "demo.pdf")
        self.assertEqual(payload["pages"], fake_pages)
        self.assertEqual(payload["tables"][0]["row_count"], 2)
        self.assertIn("document", payload["quality"])
        self.assertEqual(payload["quality_markdown"], "# Quality")

    def test_fetch_s3_markdown_endpoint(self) -> None:
        client = TestClient(server.app)

        class FakeS3Client:
            def __init__(self) -> None:
                self.download_calls: List[tuple] = []
                self.copy_calls: List[Dict[str, Any]] = []
                self.put_calls: List[Dict[str, Any]] = []

            def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
                self.download_calls.append((bucket, key, ExtraArgs))
                fileobj.write(b"%PDF-1.4 mock")

            def head_object(self, **kwargs):
                return {"Metadata": {}, "ContentType": "application/pdf"}

            def copy_object(self, **kwargs):
                self.copy_calls.append(kwargs)
                return {}

            def put_object(self, **kwargs):
                self.put_calls.append(kwargs)
                return {"VersionId": "meta-json"}

        fake_s3_client = FakeS3Client()

        fake_pipeline_result = types.SimpleNamespace(
            markdown="# sample markdown",
            text_engine="pdfminer",
            ocr_strategy=None,
            tables=[],
        )

        metadata_backup = server._metadata_generator
        server._metadata_generator = lambda _: {
            "labels": ["pharmacology"],
            "keywords": ["efficacy"],
            "language": "en",
        }

        with patch.object(server, "PDFProcessingPipeline") as mock_pipeline:
            mock_pipeline.return_value.run.return_value = fake_pipeline_result

            fake_exceptions = types.SimpleNamespace(BotoCoreError=Exception, ClientError=Exception)
            fake_botocore = types.ModuleType("botocore")
            fake_botocore.exceptions = fake_exceptions
            with patch.dict(
                sys.modules,
                {
                    "boto3": types.SimpleNamespace(client=lambda *args, **kwargs: fake_s3_client),
                    "botocore": fake_botocore,
                    "botocore.exceptions": fake_exceptions,
                },
            ):
                response = client.post(
                    "/s3/markdown",
                    json={
                        "bucket": "demo-bucket",
                        "key": "LT1009/Module 1.Quality/report.pdf",
                        "version_id": "abc123",
                        "aws_region": "us-east-1",
                    },
                )

        server._metadata_generator = metadata_backup

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["markdown"], "# sample markdown")
        self.assertEqual(data["text_engine"], "pdfminer")
        self.assertEqual(data["tables"], 0)
        self.assertEqual(data["metadata"]["labels"], ["pharmacology"])
        self.assertEqual(len(fake_s3_client.download_calls), 1)
        bucket, key, extra = fake_s3_client.download_calls[0]
        self.assertEqual(bucket, "demo-bucket")
        self.assertEqual(key, "LT1009/Module 1.Quality/report.pdf")
        self.assertEqual(extra, {"VersionId": "abc123"})
        self.assertEqual(len(fake_s3_client.copy_calls), 1)
        self.assertEqual(fake_s3_client.copy_calls[0]["Metadata"]["labels"], "pharmacology")
        self.assertEqual(len(fake_s3_client.put_calls), 1)
        self.assertTrue(fake_s3_client.put_calls[0]["Key"].endswith("report.pdf.meta.json"))

    def test_fetch_s3_markdown_summary_endpoint(self) -> None:
        client = TestClient(server.app)

        class FakeS3Client:
            def __init__(self) -> None:
                self.download_calls: List[tuple] = []

            def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
                self.download_calls.append((bucket, key, ExtraArgs))
                fileobj.write(b"%PDF-1.5 mock")

        fake_s3_client = FakeS3Client()
        fake_pipeline_result = types.SimpleNamespace(
            markdown="# sample markdown",
            text_engine="pdfminer",
            ocr_strategy=None,
            tables=[{"page_number": 1}],
        )

        class FakeSummarizer:
            def __init__(self) -> None:
                self._available = True

            def is_available(self) -> bool:
                return self._available

            def __call__(self, markdown: str) -> TopicSummaryResult:
                assert markdown == "# sample markdown"
                return TopicSummaryResult(
                    summary="Overall study summary.",
                    topics=[
                        TopicSection(title="Introduction", description="Context", anchor="introduction"),
                        TopicSection(title="Findings", description="Key outcomes", anchor="findings"),
                    ],
                )

        summary_backup = server._summary_generator
        server._summary_generator = FakeSummarizer()

        with patch.object(server, "PDFProcessingPipeline") as mock_pipeline:
            mock_pipeline.return_value.run.return_value = fake_pipeline_result

            fake_exceptions = types.SimpleNamespace(BotoCoreError=Exception, ClientError=Exception)
            fake_botocore = types.ModuleType("botocore")
            fake_botocore.exceptions = fake_exceptions
            with patch.dict(
                sys.modules,
                {
                    "boto3": types.SimpleNamespace(client=lambda *args, **kwargs: fake_s3_client),
                    "botocore": fake_botocore,
                    "botocore.exceptions": fake_exceptions,
                },
            ):
                response = client.post(
                    "/s3/markdown/summary",
                    json={
                        "bucket": "demo-bucket",
                        "key": "LT1009/Module 1.Quality/report.pdf",
                        "version_id": "abc123",
                        "aws_region": "us-east-1",
                    },
                )

        server._summary_generator = summary_backup

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("## Key Topics", data["markdown"])
        self.assertTrue(data["summary_text"].startswith("Document Summary"))
        self.assertEqual(len(data["topics"]), 2)
        self.assertEqual(data["topics"][0]["anchor"], "introduction")
        self.assertEqual(len(fake_s3_client.download_calls), 1)

    def test_save_s3_markdown_endpoint(self) -> None:
        client = TestClient(server.app)
        put_calls: List[Dict[str, Any]] = []

        def put_object(**kwargs: Any) -> Dict[str, Any]:
            put_calls.append(kwargs)
            return {"VersionId": "ver-002"}

        fake_s3_client = types.SimpleNamespace(put_object=put_object)
        fake_exceptions = types.SimpleNamespace(BotoCoreError=Exception, ClientError=Exception)
        fake_botocore = types.ModuleType("botocore")
        fake_botocore.exceptions = fake_exceptions

        with patch.dict(
            sys.modules,
            {
                "boto3": types.SimpleNamespace(client=lambda *args, **kwargs: fake_s3_client),
                "botocore": fake_botocore,
                "botocore.exceptions": fake_exceptions,
            },
        ):
            response = client.post(
                "/s3/markdown/save",
                json={
                    "bucket": "demo-bucket",
                    "path": "filynai.com/LT1009/Module 1.Quality",
                    "filename": "report",
                    "markdown": "# updated",
                    "label": "annotated",
                    "tags": {"status": "draft", "reviewer": "QA"},
                    "metadata": {"source": "editor"},
                    "aws_region": "us-east-1",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["bucket"], "demo-bucket")
        self.assertEqual(payload["key"], "filynai.com/LT1009/Module 1.Quality/report.md")
        self.assertEqual(payload["version_id"], "ver-002")
        self.assertEqual(payload["label"], "annotated")
        self.assertEqual(payload["tags"], {"status": "draft", "reviewer": "QA"})

        self.assertEqual(payload["metadata"], {"source": "editor", "label": "annotated"})

        self.assertGreaterEqual(len(put_calls), 1)
        primary_put = put_calls[0]
        self.assertEqual(primary_put["Bucket"], "demo-bucket")
        self.assertEqual(primary_put["Key"], "filynai.com/LT1009/Module 1.Quality/report.md")
        self.assertEqual(primary_put["ContentType"], "text/markdown")
        self.assertIn(b"# updated", primary_put["Body"])
        self.assertEqual(primary_put["Metadata"], {"source": "editor", "label": "annotated"})

        self.assertTrue(
            any(call.get("Key", "").endswith("report.md.meta.json") for call in put_calls),
            "Metadata JSON upload not detected",
        )


if __name__ == "__main__":
    unittest.main()
