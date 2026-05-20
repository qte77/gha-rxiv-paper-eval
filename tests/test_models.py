"""Tests for the Pydantic models and Settings introduced in PR #3a."""
from __future__ import annotations

import pytest
from eval_papers import (
    ArxivCsvRow,
    ExtractedFields,
    Paper,
    Settings,
    Verdict,
    _paper_from_arxiv_row,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Paper
# ---------------------------------------------------------------------------

_CSV_ROW: dict[str, str] = {
    "Date": "2026-04-06",
    "ISOWeek": "15",
    "DOI": "10.1101/2024.09.07.000001",
    "Version": "1",
    "Category": "microbiology",
    "Title": "Some bacterial enzyme paper",
    "Authors": "Smith, J.; Jones, A.",
}


def test_paper_roundtrip_from_csv_row() -> None:
    paper = Paper.model_validate(_CSV_ROW)
    assert paper.doi == "10.1101/2024.09.07.000001"
    assert paper.category == "microbiology"
    assert paper.model_dump(by_alias=True) == _CSV_ROW


def test_paper_accepts_python_attribute_names() -> None:
    paper = Paper(
        date="2026-04-06",
        iso_week="15",
        doi="10.1101/x",
        version="1",
        category="microbiology",
        title="t",
        authors="a",
    )
    assert paper.iso_week == "15"


def test_paper_missing_field_raises() -> None:
    bad = dict(_CSV_ROW)
    del bad["DOI"]
    with pytest.raises(ValidationError):
        Paper.model_validate(bad)


# ---------------------------------------------------------------------------
# Paper — arxiv schema adapter
# ---------------------------------------------------------------------------

_ARXIV_ROW: dict[str, str] = {
    "Published": "2024-06-13T17:59:59Z",
    "Weekday(Monday==0)": "24",
    "Updated": "2024-06-13T17:59:59Z",
    "ID": "2406.09418",
    "Version": "1",
    "Title": "'VideoGPT+: Integrating Image and Video Encoders'",
}


def test_paper_from_arxiv_row_maps_core_fields() -> None:
    paper = _paper_from_arxiv_row(_ARXIV_ROW)
    assert paper.doi == "2406.09418"
    assert paper.date == "2024-06-13"
    assert paper.version == "1"


def test_paper_from_arxiv_row_strips_quoted_title() -> None:
    paper = _paper_from_arxiv_row(_ARXIV_ROW)
    assert paper.title == "VideoGPT+: Integrating Image and Video Encoders"


def test_paper_from_arxiv_row_derives_iso_week_from_published() -> None:
    paper = _paper_from_arxiv_row(_ARXIV_ROW)
    # 2024-06-13 is in ISO week 24
    assert paper.iso_week == "24"


def test_paper_from_arxiv_row_defaults_missing_columns() -> None:
    # arxiv producer CSV has no Category / Authors columns
    paper = _paper_from_arxiv_row(_ARXIV_ROW)
    assert paper.category == ""
    assert paper.authors == ""


def test_arxiv_csv_row_missing_field_raises() -> None:
    bad = dict(_ARXIV_ROW)
    del bad["ID"]
    with pytest.raises(ValidationError):
        ArxivCsvRow.model_validate(bad)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def test_verdict_basic_construction() -> None:
    v = Verdict(doi="10.1101/x", relevant=True, raw="YES")
    assert v.relevant is True
    assert v.raw == "YES"


def test_verdict_serializes_to_dict() -> None:
    v = Verdict(doi="10.1101/x", relevant=False, raw="NO")
    payload = v.model_dump()
    assert payload == {"doi": "10.1101/x", "relevant": False, "raw": "NO"}


def test_verdict_roundtrip_through_json() -> None:
    v = Verdict(doi="10.1101/x", relevant=True, raw="YES")
    rehydrated = Verdict.model_validate_json(v.model_dump_json())
    assert rehydrated == v


# ---------------------------------------------------------------------------
# ExtractedFields
# ---------------------------------------------------------------------------


def test_extracted_fields_defaults() -> None:
    ef = ExtractedFields()
    assert ef.summary == ""
    assert ef.organisms == []
    assert ef.methods == []
    assert ef.key_findings == []
    assert ef.study_type == "other"


def test_extracted_fields_parses_valid_json() -> None:
    raw = (
        '{"summary": "Discovered X.", "organisms": ["E. coli"], '
        '"methods": ["MD"], "key_findings": ["binds Y"], "study_type": "in_silico"}'
    )
    ef = ExtractedFields.model_validate_json(raw)
    assert ef.summary == "Discovered X."
    assert ef.organisms == ["E. coli"]
    assert ef.study_type == "in_silico"


def test_extracted_fields_rejects_unknown_study_type() -> None:
    bad = '{"study_type": "speculative"}'
    with pytest.raises(ValidationError):
        ExtractedFields.model_validate_json(bad)


def test_extracted_fields_allows_extra_keys() -> None:
    # Production fallback: when the model returns prose, we surface it via _raw.
    raw = '{"summary": "s", "_raw": "garbage that wasnt parseable"}'
    ef = ExtractedFields.model_validate_json(raw)
    assert ef.summary == "s"
    assert ef.model_dump().get("_raw") == "garbage that wasnt parseable"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "RXIV_EVAL_OFFLINE",
        "RXIV_EVAL_STUB_MODE",
        "RXIV_EVAL_RETRY_MAX_ATTEMPTS",
        "RXIV_EVAL_RETRY_BASE_SECS",
        "RXIV_EVAL_NO_CACHE",
    ):
        monkeypatch.delenv(key, raising=False)

    s = Settings()
    assert s.offline is False
    assert s.stub_mode == "hash"
    assert s.retry_max_attempts == 5
    assert s.retry_base_secs == 4.0
    assert s.no_cache is False


def test_settings_reads_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RXIV_EVAL_OFFLINE", "1")
    monkeypatch.setenv("RXIV_EVAL_STUB_MODE", "yes")
    monkeypatch.setenv("RXIV_EVAL_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("RXIV_EVAL_RETRY_BASE_SECS", "0.5")
    monkeypatch.setenv("RXIV_EVAL_NO_CACHE", "1")

    s = Settings()
    assert s.offline is True
    assert s.stub_mode == "yes"
    assert s.retry_max_attempts == 2
    assert s.retry_base_secs == 0.5
    assert s.no_cache is True


def test_settings_offline_accepts_truthy_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "True", "TRUE"):
        monkeypatch.setenv("RXIV_EVAL_OFFLINE", value)
        assert Settings().offline is True, f"value {value!r} should be truthy"
