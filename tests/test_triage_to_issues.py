"""Tests for scripts/triage_to_issues.py."""
from __future__ import annotations

import triage_to_issues as tti


def test_open_issues_creates_one_per_row(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        tti,
        "create_issue",
        lambda title, body, label, repo: calls.append((title, body, label, repo)),
    )
    extracts = tmp_path / "e.jsonl"
    extracts.write_text(
        '{"doi": "10.1/a", "title": "A", "extracted": {"summary": "x"}}\n'
        '{"doi": "10.1/b", "title": "B", "extracted": {"summary": "y"}}\n',
        encoding="utf-8",
    )
    count = tti.open_issues_from_extracts(extracts, "lab", "rxiv:", "owner/repo")
    assert count == 2
    assert [c[0] for c in calls] == ["rxiv: A", "rxiv: B"]
    assert all(c[2] == "lab" for c in calls)
    assert all(c[3] == "owner/repo" for c in calls)


def test_open_issues_skips_blank_lines(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tti, "create_issue", lambda *a: calls.append(a))
    extracts = tmp_path / "e.jsonl"
    extracts.write_text(
        '{"doi": "10.1/a", "title": "A"}\n\n  \n{"doi": "10.1/b", "title": "B"}\n',
        encoding="utf-8",
    )
    count = tti.open_issues_from_extracts(extracts, "lab", "rxiv:", None)
    assert count == 2
    assert len(calls) == 2


def test_open_issues_skips_malformed_lines(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tti, "create_issue", lambda *a: calls.append(a))
    extracts = tmp_path / "e.jsonl"
    extracts.write_text(
        '{"doi": "10.1/a", "title": "A"}\nnot-json\n{"doi": "10.1/b", "title": "B"}\n',
        encoding="utf-8",
    )
    count = tti.open_issues_from_extracts(extracts, "lab", "rxiv:", None)
    assert count == 2


def test_main_invokes_open_issues(tmp_path, monkeypatch):
    extracts = tmp_path / "e.jsonl"
    extracts.write_text(
        '{"doi": "10.1/a", "title": "A", "extracted": {"summary": "x"}}\n',
        encoding="utf-8",
    )
    seen = {}
    monkeypatch.setattr(
        tti,
        "create_issue",
        lambda title, body, label, repo: seen.update(
            title=title, body=body, label=label, repo=repo
        ),
    )
    rc = tti.main(
        [
            "--extracts",
            str(extracts),
            "--label",
            "custom-label",
            "--title-prefix",
            "feed:",
            "--repo",
            "owner/repo",
        ]
    )
    assert rc == 0
    assert seen["title"] == "feed: A"
    assert seen["label"] == "custom-label"
    assert seen["repo"] == "owner/repo"
