# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
Ad-hoc runner for the LangChain + NCD pipeline.

Usage:
    poetry run python scripts/run_langchain_pipeline.py --pdf /path/to/file.pdf \
        [--use-db] [--tenant-id <uuid>] [--created-by <uuid>] [--s3-bucket local-bucket]

Defaults:
- Uses stub repositories unless --use-db is passed.
- Auto-creates documents/document_versions when --use-db is set and IDs are omitted.
- Uses bundled LangChain chains (OpenAI chat). Requires OPENAI_API_KEY if not using --stub-chains.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path

from pdf_analysis.pipeline.pipeline import PDFProcessingPipeline, PipelineContext
from pdf_analysis.pipeline.langchain_extraction import (
    LangChainExtractionPipeline,
)
from pdf_analysis.pipeline.langchain_chains import (
    build_noael_chain,
    build_pk_chain,
    build_study_segmentation_chain,
)


class StubRepo:
    """In-memory repository to avoid DB writes."""

    def __init__(self) -> None:
        self.calls = []

    def _record(self, name: str, kwargs: dict) -> str:
        ident = str(uuid.uuid4())
        self.calls.append((name, kwargs | {"id": ident}))
        return ident

    def create_extraction_run(self, **kwargs) -> str:
        return self._record("create_extraction_run", kwargs)

    def upsert_study(self, **kwargs) -> str:
        return self._record("upsert_study", kwargs)

    def insert_noael(self, **kwargs) -> str:
        return self._record("insert_noael", kwargs)

    def insert_pk_parameter(self, **kwargs) -> str:
        return self._record("insert_pk_parameter", kwargs)

    def insert_extracted_entity(self, **kwargs) -> str:
        return self._record("insert_extracted_entity", kwargs)

    def create_low_confidence_comment(self, **kwargs) -> str:
        return self._record("create_low_confidence_comment", kwargs)

    def ensure_document_and_version(self, **kwargs):
        # Stub: just return generated IDs
        doc_id = str(uuid.uuid4())
        dvid = str(uuid.uuid4())
        self.calls.append(("ensure_document_and_version", kwargs | {"document_id": doc_id, "document_version_id": dvid}))
        return doc_id, dvid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LangChain + NCD pipeline locally.")
    parser.add_argument("--pdf", required=True, type=Path, help="Path to local PDF")
    parser.add_argument("--document-version-id", default=None, help="Document version UUID (auto-created if omitted with --use-db)")
    parser.add_argument("--source-document-id", default=None, help="Source document UUID (auto-created if omitted with --use-db)")
    parser.add_argument("--created-by", default=str(uuid.uuid4()), help="User UUID for created_by fields")
    parser.add_argument("--tenant-id", default=None, help="Tenant UUID (required to auto-create documents when using --use-db)")
    parser.add_argument("--use-db", action="store_true", help="Use real DB repository (requires DATABASE_URL config)")
    parser.add_argument("--model", default="gpt-4o-mini", help="LLM model name for chains (requires API key)")
    parser.add_argument("--stub-chains", action="store_true", help="Use stub LangChain chains (no LLM calls, deterministic outputs).")
    parser.add_argument("--s3-bucket", default="local-bucket", help="Synthetic bucket name when auto-creating document_versions")
    parser.add_argument("--s3-key", default=None, help="Synthetic key when auto-creating document_versions; defaults to PDF filename")
    return parser.parse_args()


def _require_openai_api_key() -> None:
    import os

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is required when not using --stub-chains. "
            "Set the env var or run with --stub-chains."
        )


def _build_stub_chains():
    class StubChain:
        def __init__(self, payload):
            self.payload = payload

        def invoke(self, _):
            return self.payload

    seg = StubChain({"studies": [{"study_id": "STUB-1", "study_type": "repeat_dose_tox", "start_page": 1, "end_page": 3}]})
    noael = StubChain(
        {
            "items": [
                {
                    "dose": 10.0,
                    "dose_unit": "mg/kg",
                    "species": "rat",
                    "sex": "male",
                    "endpoint": "clinical observations",
                    "quote": "NOAEL at 10 mg/kg",
                    "confidence": 0.8,
                }
            ]
        }
    )
    pk = StubChain(
        {
            "items": [
                {
                    "parameter": "Cmax",
                    "value": 123.4,
                    "unit": "ng/mL",
                    "dose_group": "High",
                    "quote": "Cmax 123.4 ng/mL",
                    "confidence": 0.77,
                }
            ]
        }
    )
    return seg, noael, pk


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _infer_file_type(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    return ext if ext in {"pdf", "docx", "txt", "html"} else "pdf"


def main() -> None:
    args = parse_args()
    if not args.pdf.exists():
        raise SystemExit(f"PDF not found: {args.pdf}")

    # Stage 1: PDF processing to get chunks
    pdf_pipeline = PDFProcessingPipeline()
    result = pdf_pipeline.run(args.pdf)

    ctx = PipelineContext(pdf_path=args.pdf, pages=result.pages, tables=result.tables)
    chunks = pdf_pipeline._build_document_chunks(ctx)

    print(f"[pipeline] pages={len(result.pages)} tables={len(result.tables)} chunks={len(chunks)}")
    if chunks:
        print(f"[pipeline] first chunk page={chunks[0].page} len={len(chunks[0].text)}")

    # Stage 2: LangChain chains
    if args.stub_chains:
        seg_chain, noael_chain, pk_chain = _build_stub_chains()
        print("[chains] using stub chains (no LLM calls)")
    else:
        _require_openai_api_key()
        seg_chain = build_study_segmentation_chain(model=args.model)
        noael_chain = build_noael_chain(model=args.model)
        pk_chain = build_pk_chain(model=args.model)

    # Repository selection
    if args.use_db:
        from database.db_interface import NCDRepository

        repo = NCDRepository()
        print("[repo] using real database repository")
    else:
        repo = StubRepo()
        print("[repo] using stub repository (no DB writes)")

    # Auto-create documents/versions if needed
    document_id = args.source_document_id
    document_version_id = args.document_version_id
    if args.use_db and (document_id is None or document_version_id is None):
        if not args.tenant_id:
            raise SystemExit("tenant_id is required when auto-creating documents with --use-db")
        synthetic_key = args.s3_key or args.pdf.name
        content_hash = _sha256_file(args.pdf)
        document_id, document_version_id = repo.ensure_document_and_version(
            tenant_id=args.tenant_id,
            title=args.pdf.name,
            s3_bucket=args.s3_bucket,
            s3_key=synthetic_key,
            s3_version_id=None,
            file_type=_infer_file_type(args.pdf.name),
            content_hash=content_hash,
            page_count=len(result.pages),
            created_by=args.created_by,
        )

    lc_pipeline = LangChainExtractionPipeline(
        study_segmentation_chain=seg_chain,
        noael_chain=noael_chain,
        pk_chain=pk_chain,
        repository=repo,
    )

    persisted = lc_pipeline.run(
        document_version_id=document_version_id or args.document_version_id,
        source_document_id=document_id or args.source_document_id,
        extractor_name="langchain_local_runner",
        model_name=args.model,
        chunks=chunks,
        created_by=args.created_by,
    )

    print(json.dumps(persisted, indent=2))

    if isinstance(repo, StubRepo):
        print("\n[stub repo calls]")
        for name, payload in repo.calls:
            print(name, payload)


if __name__ == "__main__":
    main()
