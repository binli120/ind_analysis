from __future__ import annotations

from typing import Any, Dict

from pdf_analysis import sqs_worker


def test_should_skip_core_requires_sidecar(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        sqs_worker,
        "_markdown_sidecar_exists",
        lambda _bucket, _key: True,
    )
    assert sqs_worker._should_skip_core(
        run_core=True,
        force=False,
        core_status_row={"status": "completed"},
        bucket="bucket",
        key="path/file.pdf",
    )

    monkeypatch.setattr(
        sqs_worker,
        "_markdown_sidecar_exists",
        lambda _bucket, _key: False,
    )
    assert not sqs_worker._should_skip_core(
        run_core=True,
        force=False,
        core_status_row={"status": "completed"},
        bucket="bucket",
        key="path/file.pdf",
    )


def test_should_skip_core_honors_force_and_status(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        sqs_worker,
        "_markdown_sidecar_exists",
        lambda _bucket, _key: True,
    )

    assert not sqs_worker._should_skip_core(
        run_core=False,
        force=False,
        core_status_row={"status": "completed"},
        bucket="bucket",
        key="path/file.pdf",
    )
    assert not sqs_worker._should_skip_core(
        run_core=True,
        force=True,
        core_status_row={"status": "completed"},
        bucket="bucket",
        key="path/file.pdf",
    )
    assert not sqs_worker._should_skip_core(
        run_core=True,
        force=False,
        core_status_row={"status": "failed"},
        bucket="bucket",
        key="path/file.pdf",
    )
    assert not sqs_worker._should_skip_core(
        run_core=True,
        force=False,
        core_status_row=None,
        bucket="bucket",
        key="path/file.pdf",
    )


def test_markdown_sidecar_exists_checks_extracted_md(monkeypatch: Any) -> None:
    class _FakeS3:
        def __init__(self) -> None:
            self.last_call: Dict[str, str] = {}

        def head_object(self, **kwargs: Any) -> Dict[str, Any]:
            self.last_call = {
                "Bucket": str(kwargs.get("Bucket") or ""),
                "Key": str(kwargs.get("Key") or ""),
            }
            return {"ok": True}

    fake = _FakeS3()
    monkeypatch.setattr(sqs_worker, "s3", fake)

    assert sqs_worker._markdown_sidecar_exists("bucket", "path/file.pdf")
    assert fake.last_call["Bucket"] == "bucket"
    assert fake.last_call["Key"] == "path/file.pdf.extracted.md"


def test_s3_object_exists_returns_false_on_head_failure(monkeypatch: Any) -> None:
    class _FakeS3:
        def head_object(self, **_kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("nope")

    monkeypatch.setattr(sqs_worker, "s3", _FakeS3())
    assert not sqs_worker._s3_object_exists("bucket", "key")
