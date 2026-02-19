import importlib.util
import sys
import types
import warnings
from pathlib import Path

from pdf_analysis.api.constants import STUDY_ID_SKIP_RE


def _load_ingest_module():
    injected_modules: list[str] = []
    if "boto3" not in sys.modules:
        boto3_stub = types.ModuleType("boto3")
        boto3_stub.client = lambda *args, **kwargs: None
        sys.modules["boto3"] = boto3_stub
        injected_modules.append("boto3")

    if "botocore.exceptions" not in sys.modules:
        botocore_module = sys.modules.get("botocore")
        if botocore_module is None:
            botocore_module = types.ModuleType("botocore")
            sys.modules["botocore"] = botocore_module
            injected_modules.append("botocore")

        botocore_exceptions = types.ModuleType("botocore.exceptions")

        class _ClientError(Exception):
            pass

        botocore_exceptions.ClientError = _ClientError
        sys.modules["botocore.exceptions"] = botocore_exceptions
        botocore_module.exceptions = botocore_exceptions
        injected_modules.append("botocore.exceptions")

    path = Path("scripts/ingest_module4_batch.py").resolve()
    spec = importlib.util.spec_from_file_location("ingest_module4_batch", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load ingest_module4_batch module")
    module = importlib.util.module_from_spec(spec)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"builtin type (SwigPyPacked|SwigPyObject|swigvarlink) has no __module__ attribute",
                category=DeprecationWarning,
            )
            spec.loader.exec_module(module)
        return module
    finally:
        for module_name in reversed(injected_modules):
            sys.modules.pop(module_name, None)


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


def test_collect_study_number_aliases_dedupes_and_keeps_order():
    mod = _load_ingest_module()
    aliases = mod._collect_study_number_aliases(
        "RP-PC-41",
        "301644",
        "rp-pc-41",
        "",
        "301644",
    )
    assert aliases == ["RP-PC-41", "301644"]


def test_extract_study_identifiers_keeps_sponsor_and_cro_ids():
    mod = _load_ingest_module()
    payload = mod._extract_study_identifiers(
        text="Sponsor Study Number: RP-PC-41\nCRO Study Number: 301644",
        file_name="report.pdf",
        content_hash="abcdef1234567890",
    )
    assert payload["study_number"] == "RP-PC-41"
    assert payload["sponsor_study_number"] == "RP-PC-41"
    assert payload["cro_study_number"] == "301644"


def test_extract_testing_facility_from_sentence_pattern():
    mod = _load_ingest_module()
    text = "This study was conducted at Covance Laboratories Inc. in Madison, Wisconsin."
    value = mod._extract_testing_facility(text)
    assert value.startswith("Covance Laboratories Inc")
    assert "Madison" in value


def test_extract_testing_facility_from_testing_facility_was_pattern():
    mod = _load_ingest_module()
    text = "Testing Facility was WuXi AppTec Co., Ltd."
    assert mod._extract_testing_facility(text).startswith("WuXi AppTec Co., Ltd")


def test_extract_testing_facility_rejects_dose_phrase():
    mod = _load_ingest_module()
    text = "The study was conducted at 10 mg/kg/day in rats."
    assert mod._extract_testing_facility(text) == ""
