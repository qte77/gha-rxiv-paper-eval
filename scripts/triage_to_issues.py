#!/usr/bin/env python3
"""Open one GitHub issue per record in `extracts.jsonl`.

Designed to run as the second stage of a consumer's eval workflow, after
`eval-papers.yaml` uploads its artifact. Invoked by the reusable
`.github/workflows/triage-to-issues.yaml`, but runnable standalone against
any local `extracts.jsonl` for smoke tests.

Auth is delegated to the `gh` CLI on PATH (uses `GH_TOKEN` from the env);
the calling workflow must grant `permissions: issues: write`.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LABEL = "rxiv-feed"
DEFAULT_TITLE_PREFIX = "rxiv:"


def build_issue_title(record: dict[str, Any], prefix: str) -> str:
    """Compose `{prefix} {paper title}`; fall back when title is missing."""
    title = record.get("title") or "(untitled)"
    return f"{prefix} {title}"


def build_issue_body(record: dict[str, Any]) -> str:
    """Compose issue body from doi + extracted.summary (both may be missing)."""
    doi = record.get("doi", "")
    extracted = record.get("extracted") or {}
    summary = extracted.get("summary") or ""
    return f"DOI: https://doi.org/{doi}\n\n{summary}"


def create_issue(title: str, body: str, label: str, repo: str | None) -> None:
    """Invoke `gh issue create` (list form, no shell)."""
    # S603/S607: list-form invocation, no shell. `gh` is provided by the
    # GitHub Actions runner's PATH and authenticated via GH_TOKEN.
    cmd = ["gh", "issue", "create", "--title", title, "--body", body, "--label", label]
    if repo:
        cmd += ["--repo", repo]
    subprocess.run(cmd, check=True)  # noqa: S603


def open_issues_from_extracts(
    extracts_path: Path,
    label: str,
    title_prefix: str,
    repo: str | None,
) -> int:
    """Walk `extracts.jsonl` and open one issue per row. Returns count opened."""
    count = 0
    with extracts_path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping malformed JSONL line %d in %s", line_no, extracts_path
                )
                continue
            title = build_issue_title(record, title_prefix)
            body = build_issue_body(record)
            create_issue(title, body, label, repo)
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    """Parse args, open issues, return exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extracts", required=True, type=Path, help="Path to extracts.jsonl"
    )
    parser.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help=f"Label applied to each created issue (default: {DEFAULT_LABEL})",
    )
    parser.add_argument(
        "--title-prefix",
        default=DEFAULT_TITLE_PREFIX,
        help=f'Issue title prefix (default: "{DEFAULT_TITLE_PREFIX}")',
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/repo override; default = the gh context (calling repo)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    count = open_issues_from_extracts(
        args.extracts, args.label, args.title_prefix, args.repo
    )
    logger.info("Opened %d issues from %s", count, args.extracts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
