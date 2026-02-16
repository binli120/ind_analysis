import importlib.util
from pathlib import Path

from pdf_analysis.api.constants import STUDY_ID_SKIP_RE


def _load_ingest_module():
    path = Path("scripts/ingest_module4_batch.py").resolve()
    spec = importlib.util.spec_from_file_location("ingest_module4_batch", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load ingest_module4_batch module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_pharm_overview_metadata_from_title_page():
    mod = _load_ingest_module()
    markdown = """# Sample Report
## Page 1 <ref1>
Study Number: 306D326.1
Test System: Female C57BL/6 mice; Matrigel plug model supplemented with FGF or VEGF
Method of Administration: Intraperitoneal injection, every other day (q2d)
Testing Facility: BioTest Labs, Inc.
"""
    metadata = mod._extract_pharm_overview_metadata(
        key="filynai.com/ASONEP2/Module 4 Nonclinical Study Reports/4.2 Study reports/4.2.1 Pharmacology/4.2.1.1 Primary Pharmacodynamics/example.pdf",
        file_name="example.pdf",
        module4_section="4.2.1.1",
        markdown=markdown,
        content_hash="abcdef1234567890",
    )

    assert metadata["study_number"] == "306D326.1"
    assert metadata["test_system"].startswith("Female C57BL/6 mice")
    assert metadata["method_of_administration"].startswith("Intraperitoneal injection")
    assert metadata["testing_facility"] == "BioTest Labs, Inc."
    assert metadata["type_of_study"] == "Primary pharmacodynamics"


def test_study_id_skip_regex_filters_table_artifact_ids():
    assert STUDY_ID_SKIP_RE.match("input.p10.t1")
    assert STUDY_ID_SKIP_RE.match("p2.t3")
