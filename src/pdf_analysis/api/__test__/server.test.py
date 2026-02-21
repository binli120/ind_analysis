# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.api.server helpers."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types
from typing import Any, Dict

import pandas as pd
import pytest


def _ensure_optional_stubs() -> None:
    try:
        import boto3  # noqa: F401
    except Exception:
        boto3_stub = types.ModuleType("boto3")

        def _client(*_args: Any, **_kwargs: Any) -> Any:
            return object()

        boto3_stub.client = _client  # type: ignore[attr-defined]
        sys.modules["boto3"] = boto3_stub

    try:
        from botocore.exceptions import ClientError  # noqa: F401
    except Exception:
        botocore_stub = types.ModuleType("botocore")
        exceptions_stub = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            pass

        exceptions_stub.ClientError = ClientError  # type: ignore[attr-defined]
        sys.modules["botocore"] = botocore_stub
        sys.modules["botocore.exceptions"] = exceptions_stub


def _load_server_module() -> Any:
    _ensure_optional_stubs()
    module_path = Path(__file__).resolve().parents[1] / "server.py"
    spec = spec_from_file_location("pdf_analysis_api_server", module_path)
    assert spec is not None
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server() -> Any:
    return _load_server_module()


def test_table_to_payload_limits_rows(server: Any) -> None:
    df = pd.DataFrame(
        [
            {"A": 1, "B": None},
            {"A": 2, "B": "text"},
        ]
    )
    table = {
        "page_number": 3,
        "index_on_page": 1,
        "engine": "pdfplumber",
        "dataframe": df,
    }
    payload = server._table_to_payload(table, max_rows=1)

    assert payload["page_number"] == 3
    assert payload["row_count"] == 2
    assert payload["columns"] == ["A", "B"]
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["B"] == ""


def test_build_table_manifest_preview_rows(server: Any) -> None:
    df = pd.DataFrame([{"A": "x"}, {"A": "y"}])
    table = {
        "page_number": 1,
        "index_on_page": 1,
        "engine": "pdfplumber",
        "dataframe": df,
    }
    manifest = server._build_table_manifest([table], preview_rows=1)

    assert len(manifest) == 1
    preview = manifest[0]["preview_rows"]
    assert list(preview["A"]) == ["x"]


def test_parse_columns_header(server: Any) -> None:
    assert server._parse_columns_header("A; B ; ; C") == ["A", "B", "C"]
    assert server._parse_columns_header(None) == []


def test_normalize_optional_uuid(server: Any) -> None:
    assert server._normalize_optional_uuid(None, "value") is None
    assert server._normalize_optional_uuid("", "value") is None
    with pytest.raises(Exception):
        server._normalize_optional_uuid("not-a-uuid", "value")
    valid = "6fa459ea-ee8a-3ca4-894e-db77e160355e"
    assert server._normalize_optional_uuid(valid, "value") == valid


def test_format_embedding_for_prompt(server: Any) -> None:
    assert server._format_embedding_for_prompt(None) == ""
    assert server._format_embedding_for_prompt([]) == ""
    result = server._format_embedding_for_prompt([0.1, 0.2])
    assert result.startswith("[") and result.endswith("]")


def test_gap_key_mentions_module4_section(server: Any) -> None:
    assert server._gap_key_mentions_module4_section(
        "filynai.com/demo/4.2.1.1/report.pdf",
        "4.2.1.1",
    )
    assert server._gap_key_mentions_module4_section(
        "filynai.com/demo/4211-primary-pd/report.pdf",
        "4.2.1.1",
    )
    assert not server._gap_key_mentions_module4_section(
        "filynai.com/demo/4.2.2.1/report.pdf",
        "4.2.1.1",
    )


def test_gap_collect_required_fields(server: Any) -> None:
    entry = {
        "required_content": ["Study ID", "Synopsis"],
        "required_parameters": ["Cmax", "AUC"],
        "validation_rules": {"hERG_required": True},
        "extraction_focus": {"pk": {"single_dose": ["Tmax"]}},
    }
    fields = server._gap_collect_required_fields(entry)
    assert "Study ID" in fields
    assert "AUC" in fields
    assert "hERG required" in fields
    assert "single dose" in fields


def test_gap_field_is_present(server: Any) -> None:
    corpus = "Study ID LT3114-PHA-001 includes Cmax and AUC observations."
    assert server._gap_field_is_present("Study ID", corpus)
    assert server._gap_field_is_present("AUC", corpus)
    assert not server._gap_field_is_present("Respiratory system effects", corpus)


def test_gap_parse_module_number(server: Any) -> None:
    assert server._gap_parse_module_number("Module 1. Administrative") == "1"
    assert server._gap_parse_module_number("module4_nonclinical") == "4"
    assert server._gap_parse_module_number("5") == "5"
    assert server._gap_parse_module_number("no-module") is None


def test_repair_overview_table_populates_from_ncd_payload(server: Any) -> None:
    tables = [
        {
            "subsection": "2.6.3.1",
            "columns": [
                "Type of Study",
                "Test System",
                "Method of Administration",
                "Testing Facility",
                "Study Number",
                "Location in CTD",
            ],
            "rows": [],
        }
    ]
    context = {
        "mapping": [
            {
                "module4_section": "4.2.1.1",
                "category": "Primary pharmacodynamics",
            }
        ],
        "ncd_payload": {
            "studies": [
                {
                    "sponsor_study_id": "LT3114-PHA-014-R",
                    "module4_section": "4.2.1.1",
                    "species": "Female C57BL/6 mice",
                    "strain": "",
                    "route": "Intraperitoneal injection, q2d",
                    "main_source_document_id": "doc-1",
                    "extra_attributes": {"testing_facility": "ACME Labs"},
                }
            ],
            "source_documents": [
                {
                    "id": "doc-1",
                    "file_name": "FGF_VEGF_Matrigel_Primary_PD.pdf",
                    "ctd_section": "4.2.1.1",
                    "module": "Module 4",
                }
            ],
        },
        "table_assets": [],
        "section_sources": [],
        "document_keys": [],
        "ncd_study_ids": [],
        "s3_listing_keys": [],
    }

    server._repair_overview_table(tables, context)

    rows = tables[0]["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["Study Number"] == "LT3114-PHA-014-R"
    assert row["Type of Study"] == "Primary pharmacodynamics"
    assert row["Test System"] == "Female C57BL/6 mice"
    assert row["Method of Administration"] == "Intraperitoneal injection, q2d"
    assert row["Testing Facility"] == "ACME Labs"
    assert row["Location in CTD"].startswith("Module 4, Section 4.2.1.1")


def test_repair_overview_table_drops_invalid_study_numbers(server: Any) -> None:
    tables = [
        {
            "subsection": "2.6.3.1",
            "columns": [
                "Type of Study",
                "Test System",
                "Method of Administration",
                "Testing Facility",
                "Study Number",
                "Location in CTD",
            ],
            "rows": [
                {
                    "Type of Study": "",
                    "Test System": "",
                    "Method of Administration": "",
                    "Testing Facility": "",
                    "Study Number": "u20134.8-fold",
                    "Location in CTD": "",
                }
            ],
        }
    ]
    context = {
        "mapping": [],
        "ncd_payload": {},
        "table_assets": [],
        "section_sources": [],
        "document_keys": [],
        "ncd_study_ids": ["LT3114-PHA-001-R"],
        "s3_listing_keys": [],
    }

    server._repair_overview_table(tables, context)

    rows = tables[0]["rows"]
    assert len(rows) == 1
    assert rows[0]["Study Number"] == "LT3114-PHA-001-R"


def test_sections_with_material_data_skips_missing_module4_section(server: Any) -> None:
    sections = server._sections_with_material_data(
        ["4.2.1.1", "4.2.1.4"],
        section_sources=[{"section_number": "4.2.1.1"}],
        document_keys=[],
        ncd_payload={},
    )
    assert sections == ["4.2.1.1"]


def test_filter_section_entries_for_targets(server: Any) -> None:
    entries = [
        {"section": "2.6.3", "subsection": "2.6.3.1"},
        {"section": "2.6.3", "subsection": "2.6.3.4"},
        {"section": "2.6.3", "subsection": "2.6.3.5"},
    ]
    filtered = server._filter_section_entries_for_targets(
        entries,
        target_sections={"2.6.3.1", "2.6.3.4"},
    )
    assert [entry["subsection"] for entry in filtered] == ["2.6.3.1", "2.6.3.4"]


def test_extract_ncd_study_records_keeps_nonregex_sponsor_study_id(server: Any) -> None:
    context = {
        "mapping": [{"module4_section": "4.2.1.3", "category": "Safety Pharmacology"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "WKP00013 Page 2",
                    "module4_section": "4.2.1.3",
                    "species": "Cynomolgus monkey",
                    "strain": "",
                    "route": "IV",
                    "extra_attributes": {},
                }
            ],
            "source_documents": [],
        },
        "document_keys": [],
        "project_document_keys": [],
    }

    rows = server._extract_ncd_study_records(context, module4_section="4.2.1.3")
    assert len(rows) == 1
    assert rows[0]["study_number"] == "WKP00013 Page 2"


def test_extract_ncd_study_records_keeps_numeric_sponsor_study_id(server: Any) -> None:
    context = {
        "mapping": [{"module4_section": "4.2.1.3", "category": "Safety Pharmacology"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "600210",
                    "module4_section": "4.2.1.3",
                    "species": "human blood",
                    "strain": "",
                    "route": "",
                    "extra_attributes": {},
                }
            ],
            "source_documents": [],
        },
        "document_keys": [],
        "project_document_keys": [],
    }

    rows = server._extract_ncd_study_records(context, module4_section="4.2.1.3")
    assert len(rows) == 1
    assert rows[0]["study_number"] == "600210"


def test_extract_ncd_study_records_keeps_numeric_hyphen_sponsor_study_id(
    server: Any,
) -> None:
    context = {
        "mapping": [{"module4_section": "4.2.1.3", "category": "Safety Pharmacology"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "1006-2525",
                    "module4_section": "4.2.1.3",
                    "species": "mouse",
                    "strain": "CD-1",
                    "route": "IP",
                    "extra_attributes": {},
                }
            ],
            "source_documents": [],
        },
        "document_keys": [],
        "project_document_keys": [],
    }

    rows = server._extract_ncd_study_records(context, module4_section="4.2.1.3")
    assert len(rows) == 1
    assert rows[0]["study_number"] == "1006-2525"


def test_rows_from_token_candidates_keeps_unparsed_study_ids(server: Any) -> None:
    columns = ["Study Number", "Organ Systems Evaluated"]
    candidates = [
        {"study number": "RP-PC-60", "organ systems evaluated": "cardiovascular"},
        {"study number": "WKP00013 Page 2", "organ systems evaluated": "respiratory"},
    ]
    rows = server._rows_from_token_candidates(columns, candidates)
    study_numbers = [str(row.get("Study Number") or "") for row in rows]
    assert "RP-PC-60" in study_numbers
    assert "WKP00013 Page 2" in study_numbers


def test_render_dose_group_summary_formats_control_and_group_counts(server: Any) -> None:
    doses_text, sex_text = server._render_dose_group_summary(
        [
            {"name": "Vehicle control", "dose_mg_per_kg": "0.0", "sex": "M", "n_animals": 4},
            {"name": "Low", "dose_mg_per_kg": "10.0", "sex": "M", "n_animals": 4},
            {"name": "Mid", "dose_mg_per_kg": "30.0", "sex": "M", "n_animals": 4},
            {"name": "High", "dose_mg_per_kg": "100.0", "sex": "M", "n_animals": 4},
        ]
    )
    assert doses_text == "10, 30, 100 (plus vehicle control)"
    assert sex_text == "Male, n=4 (1 group)"


def test_repair_safety_table_merges_missing_cells_without_overwriting_findings(
    server: Any,
) -> None:
    tables = [
        {
            "subsection": "2.6.3.4",
            "columns": [
                "Organ Systems Evaluated",
                "Species/Strain",
                "Method of Admin.",
                "Doses (mg/kg)",
                "Gender and No. per Group",
                "Noteworthy Findings",
                "GLP Compliance",
                "Study Number",
                "Location in CTD",
            ],
            "rows": [
                {
                    "Organ Systems Evaluated": "",
                    "Species/Strain": "Cynomolgus monkey",
                    "Method of Admin.": "",
                    "Doses (mg/kg)": "10, 30, 100 (plus vehicle control)",
                    "Gender and No. per Group": "",
                    "Noteworthy Findings": (
                        "No treatment-related changes in ECG or respiratory parameters; "
                        "slight BP and HR increases at 100 mg/kg were not toxicologically meaningful."
                    ),
                    "GLP Compliance": "",
                    "Study Number": "RP-PC-41",
                    "Location in CTD": "Module 4.2.1.3",
                }
            ],
        }
    ]
    context = {
        "mapping": [{"module4_section": "4.2.1.3", "category": "Safety Pharmacology"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "RP-PC-41",
                    "module4_section": "4.2.1.3",
                    "species": "Cynomolgus monkey",
                    "strain": "",
                    "route": "IV infusion",
                    "extra_attributes": {
                        "organ_systems": "Cardiovascular; Respiratory",
                        "method_of_administration": (
                            "IV infusion to vascular access port (VAP), ~30 min"
                        ),
                        "study_number_display": (
                            "Sponsor study #: RP-PC-41; CRO study #: WKP00013"
                        ),
                        "glp_compliance": "GLP",
                    },
                }
            ],
            "dose_groups": [
                {"study_id": "study-1", "name": "Vehicle control", "dose_mg_per_kg": "0", "sex": "M", "n_animals": 4},
                {"study_id": "study-1", "name": "Low", "dose_mg_per_kg": "10", "sex": "M", "n_animals": 4},
                {"study_id": "study-1", "name": "Mid", "dose_mg_per_kg": "30", "sex": "M", "n_animals": 4},
                {"study_id": "study-1", "name": "High", "dose_mg_per_kg": "100", "sex": "M", "n_animals": 4},
            ],
            "findings": [
                {
                    "study_id": "study-1",
                    "organ_system": "Cardiovascular",
                    "finding_term": "Increased mean arterial blood pressure",
                }
            ],
            "safety_summaries": [
                {"study_id": "study-1", "limiting_finding": "No toxicologically meaningful findings"}
            ],
            "source_documents": [],
        },
        "table_assets": [],
        "section_sources": [
            {
                "section_number": "4.2.1.3",
                "summary_text": (
                    "No treatment-related changes in ECG intervals, respiratory rate, or blood gases. "
                    "At 100 mg/kg, slight increases in blood pressure and heart rate were not "
                    "toxicologically meaningful."
                ),
            }
        ],
        "document_keys": [],
        "project_document_keys": [],
    }

    server._repair_safety_pharmacology_table(tables, context)

    row = tables[0]["rows"][0]
    assert row["Noteworthy Findings"].startswith("No treatment-related changes")
    assert row["Organ Systems Evaluated"] == "Cardiovascular; Respiratory"
    assert row["Method of Admin."] == "IV infusion to vascular access port (VAP), ~30 min"
    assert row["Gender and No. per Group"] == "Male, n=4 (1 group)"
    assert row["GLP Compliance"] == "GLP"
    assert row["Study Number"] == "Sponsor study #: RP-PC-41; CRO study #: WKP00013"


def test_extract_safety_candidates_infers_organ_systems_from_safety_text(
    server: Any,
) -> None:
    context = {
        "mapping": [{"module4_section": "4.2.1.3", "category": "Safety Pharmacology"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "600210",
                    "module4_section": "4.2.1.3",
                    "species": "dog",
                    "strain": "Beagle",
                    "route": "PO",
                    "extra_attributes": {
                        "key_findings": (
                            "No treatment-related effects on ECG/QTc, respiratory rate, "
                            "or functional observational battery parameters."
                        )
                    },
                }
            ],
            "findings": [],
            "safety_summaries": [],
            "dose_groups": [],
            "source_documents": [],
        },
        "table_assets": [],
        "section_sources": [],
        "document_keys": [],
        "project_document_keys": [],
    }

    candidates = server._extract_safety_pharmacology_candidates(context)
    assert len(candidates) == 1
    assert candidates[0]["study number"] == "600210"
    assert candidates[0]["organ systems evaluated"] == "Cardiovascular; CNS; Respiratory"


def test_extract_safety_candidates_reads_report_study_no_and_infers_organ_systems(
    server: Any,
) -> None:
    context = {
        "mapping": [{"module4_section": "4.2.1.3", "category": "Safety Pharmacology"}],
        "ncd_payload": {"studies": [], "source_documents": []},
        "table_assets": [
            {
                "s3_key": (
                    "filynai.com/demo/Module 4 Nonclinical Study Reports/4.2 Study Reports/"
                    "4.2.1 Pharmacology/4.2.1.3 Safety Pharmacology/report.pdf.tables/1.json"
                ),
                "caption": "",
                "description": "",
                "keywords": [],
                "preview_rows": [
                    {
                        "Report/Study No.": "1006-2525",
                        "Species/Strain": "Beagle dog",
                        "Key Findings": (
                            "No meaningful ECG or QTc changes and no respiratory effects."
                        ),
                    }
                ],
            }
        ],
    }

    candidates = server._extract_safety_pharmacology_candidates(context)
    assert len(candidates) == 1
    assert candidates[0]["study number"] == "1006-2525"
    assert candidates[0]["organ systems evaluated"] == "Cardiovascular; Respiratory"


def test_extract_safety_candidates_infers_non_glp_from_narrative(
    server: Any,
) -> None:
    context = {
        "mapping": [{"module4_section": "4.2.1.3", "category": "Safety Pharmacology"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "RP-PC-60",
                    "module4_section": "4.2.1.3",
                    "species": "dog",
                    "strain": "Beagle",
                    "route": "PO",
                    "extra_attributes": {
                        "key_findings": (
                            "This non-GLP safety pharmacology study showed no clinically "
                            "meaningful ECG changes."
                        )
                    },
                }
            ],
            "findings": [],
            "safety_summaries": [],
            "dose_groups": [],
            "source_documents": [],
        },
        "table_assets": [],
        "section_sources": [],
        "document_keys": [],
        "project_document_keys": [],
    }

    candidates = server._extract_safety_pharmacology_candidates(context)
    assert len(candidates) == 1
    assert candidates[0]["study number"] == "RP-PC-60"
    assert candidates[0]["glp compliance"] == "Non-GLP"


def test_extract_primary_pd_candidates_infers_glp_from_qa_statement(
    server: Any,
) -> None:
    context = {
        "mapping": [{"module4_section": "4.2.1.1", "category": "Primary Pharmacodynamics"}],
        "ncd_payload": {"studies": [], "source_documents": []},
        "table_assets": [
            {
                "s3_key": (
                    "filynai.com/demo/Module 4 Nonclinical Study Reports/4.2 Study Reports/"
                    "4.2.1 Pharmacology/4.2.1.1 Primary Pharmacodynamics/report.pdf.tables/1.json"
                ),
                "caption": "",
                "description": "",
                "keywords": [],
                "preview_rows": [
                    {
                        "Type of Study": "Tumor model efficacy",
                        "Species/Strain": "Mouse, CD-1",
                        "Study No.": "306D326.1",
                        "QA Statement": "Conducted in compliance with GLP",
                    }
                ],
            }
        ],
    }

    candidates = server._extract_primary_pd_candidates(context)
    assert len(candidates) == 1
    assert candidates[0]["study number"] == "306D326.1"
    assert candidates[0]["glp compliance"] == "GLP"


def test_repair_primary_pd_table_merges_asset_fields_for_existing_ncd_row(
    server: Any,
) -> None:
    tables = [
        {
            "subsection": "2.6.3.2",
            "columns": [
                "Study Number",
                "Species/Strain or Test System",
                "Method of Administration",
                "Dose/Concentration",
                "Endpoints/Assays",
                "Key Findings",
                "GLP Compliance",
                "Location in CTD",
            ],
            "rows": [
                {
                    "Study Number": "RP-PC-21",
                    "Species/Strain or Test System": "Mice",
                    "Method of Administration": "intraperitoneal",
                    "Dose/Concentration": "",
                    "Endpoints/Assays": "",
                    "Key Findings": "",
                    "GLP Compliance": "",
                    "Location in CTD": (
                        'Module 4, Section 4.2.1.1: "LT1002 efficacy study in ovarian SKOV3 mice"'
                    ),
                }
            ],
        }
    ]

    context = {
        "mapping": [{"module4_section": "4.2.1.1", "category": "Primary Pharmacodynamics"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "RP-PC-21",
                    "module4_section": "4.2.1.1",
                    "species": "Mice",
                    "strain": "",
                    "route": "intraperitoneal",
                    "extra_attributes": {},
                }
            ],
            "dose_groups": [],
            "source_documents": [],
        },
        "table_assets": [
            {
                "s3_key": (
                    "filynai.com/demo/Module 4 Nonclinical Study Reports/4.2 Study Reports/"
                    "4.2.1 Pharmacology/4.2.1.1 Primary Pharmacodynamics/report.pdf.tables/3.json"
                ),
                "caption": "Primary pharmacology summary",
                "description": "",
                "keywords": ["primary pharmacology"],
                "preview_rows": [
                    {
                        "Study No.": "RP-PC-21",
                        "Dose/Concentration": "5, 10 mg/kg",
                        "Endpoints/Assays": "Tumor volume and body weight",
                        "Conclusions": "Dose-dependent inhibition of tumor growth",
                        "QA Statement": "This was a non-GLP exploratory study.",
                    }
                ],
            }
        ],
    }

    server._repair_primary_pharmacodynamics_table(tables, context)

    row = tables[0]["rows"][0]
    assert row["Study Number"] == "RP-PC-21"
    assert row["Dose/Concentration"] == "5, 10 mg/kg"
    assert row["Endpoints/Assays"] == "Tumor volume and body weight"
    assert row["Key Findings"] == "Dose-dependent inhibition of tumor growth"
    assert row["GLP Compliance"] == "Non-GLP"


def test_repair_primary_pd_table_uses_asset_study_id_when_row_lacks_study_number(
    server: Any,
) -> None:
    tables = [
        {
            "subsection": "2.6.3.2",
            "columns": [
                "Study Number",
                "Species/Strain or Test System",
                "Method of Administration",
                "Dose/Concentration",
                "Endpoints/Assays",
                "Key Findings",
                "GLP Compliance",
                "Location in CTD",
            ],
            "rows": [
                {
                    "Study Number": "RP-PC-22",
                    "Species/Strain or Test System": "Mice",
                    "Method of Administration": "intraperitoneal",
                    "Dose/Concentration": "",
                    "Endpoints/Assays": "",
                    "Key Findings": "",
                    "GLP Compliance": "",
                    "Location in CTD": "",
                }
            ],
        }
    ]
    context = {
        "mapping": [{"module4_section": "4.2.1.1", "category": "Primary Pharmacodynamics"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "RP-PC-22",
                    "module4_section": "4.2.1.1",
                    "species": "Mice",
                    "strain": "",
                    "route": "intraperitoneal",
                    "extra_attributes": {},
                }
            ],
            "dose_groups": [],
            "source_documents": [],
        },
        "table_assets": [
            {
                "s3_key": (
                    "filynai.com/demo/Module 4 Nonclinical Study Reports/4.2 Study Reports/"
                    "4.2.1 Pharmacology/4.2.1.1 Primary Pharmacodynamics/RP-PC-22.pdf.tables/1.json"
                ),
                "caption": "RP-PC-22 efficacy table",
                "description": "",
                "keywords": ["primary pharmacology"],
                "preview_rows": [
                    {
                        "Dose Levels": "3, 10, 30 mg/kg",
                        "Endpoint Assay": "Tumor burden and survival",
                        "Result Summary": "Significant efficacy at >=10 mg/kg",
                        "QA Statement": "GLP compliant",
                    }
                ],
            }
        ],
    }

    server._repair_primary_pharmacodynamics_table(tables, context)
    row = tables[0]["rows"][0]
    assert row["Study Number"] == "RP-PC-22"
    assert row["Dose/Concentration"] == "3, 10, 30 mg/kg"
    assert row["Endpoints/Assays"] == "Tumor burden and survival"
    assert row["Key Findings"] == "Significant efficacy at >=10 mg/kg"
    assert row["GLP Compliance"] == "GLP"


def test_repair_primary_pd_table_infers_fields_from_location_title_when_missing(
    server: Any,
) -> None:
    tables = [
        {
            "subsection": "2.6.3.2",
            "columns": [
                "Study Number",
                "Species/Strain or Test System",
                "Method of Administration",
                "Dose/Concentration",
                "Endpoints/Assays",
                "Key Findings",
                "GLP Compliance",
                "Location in CTD",
            ],
            "rows": [],
        }
    ]
    context = {
        "mapping": [{"module4_section": "4.2.1.1", "category": "Primary Pharmacodynamics"}],
        "ncd_payload": {
            "studies": [
                {
                    "id": "study-1",
                    "sponsor_study_id": "RP-PC-30",
                    "module4_section": "4.2.1.1",
                    "species": "Mice",
                    "strain": "",
                    "route": "intraperitoneal",
                    "extra_attributes": {
                        "study_title": (
                            "LT1002 administered by intraperitoneal injection inhibits "
                            "growth of established ovarian xenografts in mice"
                        ),
                        "location_in_ctd": (
                            'Module 4, Section 4.2.1.1: "LT1002 administered by intraperitoneal '
                            'injection inhibits growth of established ovarian xenografts in mice"'
                        ),
                    },
                }
            ],
            "dose_groups": [],
            "source_documents": [],
        },
        "table_assets": [],
        "section_sources": [],
    }

    server._repair_primary_pharmacodynamics_table(tables, context)
    assert len(tables[0]["rows"]) == 1
    row = tables[0]["rows"][0]
    assert row["Study Number"] == "RP-PC-30"
    assert row["Endpoints/Assays"] == "Tumor growth/volume"
    assert row["Key Findings"].lower().startswith("lt1002 administered")
    assert row["GLP Compliance"] == "Not reported"
