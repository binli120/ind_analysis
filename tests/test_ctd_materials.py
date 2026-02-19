from __future__ import annotations

from typing import Any, Dict, List

from ncd.types.ctd_materials import fetch_study_ids_for_sections


class _FakeResult:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_FakeResult":
        return self

    def all(self) -> List[Dict[str, Any]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.statement = ""
        self.params: Dict[str, Any] = {}

    def execute(self, statement: Any, params: Dict[str, Any]) -> _FakeResult:
        self.statement = str(statement)
        self.params = params
        return _FakeResult(self._rows)


def test_fetch_study_ids_for_sections_returns_study_id_rows() -> None:
    db = _FakeSession([{"study_id": "RP-PC-41"}, {"study_id": "301644"}])

    result = fetch_study_ids_for_sections(
        db,
        project_id="project-1",
        module4_sections=["4.2.1.1"],
    )

    assert result == ["RP-PC-41", "301644"]
    assert db.params["pid"] == "project-1"
    assert db.params["sections"] == ["4.2.1.1"]


def test_fetch_study_ids_for_sections_query_includes_alias_sources() -> None:
    db = _FakeSession([])

    fetch_study_ids_for_sections(
        db,
        project_id="project-1",
        module4_sections=["4.2.1.1"],
    )

    assert "cro_study_number" in db.statement
    assert "sponsor_study_number" in db.statement
    assert "study_number_aliases" in db.statement
