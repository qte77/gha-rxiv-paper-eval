#!/usr/bin/env python3
"""Evaluate a weekly preprint CSV via the GitHub Models REST API.

Pipeline:
    1. Fetch data/<server>/<year>/<week>.csv from the feed repo.
    2. Optional cheap pre-filter on category allowlist + max_papers cap.
    3. LLM relevance pass (YES/NO, temperature 0) per row -> relevant.csv.
    4. Optional enrichment: fetch abstract from rxiv API + structured extraction
       -> extracts.jsonl.
    5. Write summary.md.

Designed to be invoked from .github/workflows/eval-papers.yaml.
External services: `gh` CLI for the feed-CSV fetch (uses GH_TOKEN), and a
direct HTTPS POST to https://models.github.ai/inference/chat/completions for
inference (also uses GH_TOKEN; requires the `models: read` scope).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Literal

# FIXME: drop defusedxml + Atom parsing once the producer (gha-rxiv-feed-action)
# emits a normalized arxiv CSV that already carries the abstract. See
# docs/design.md "Servers and schema adapters".
import defusedxml.ElementTree as ET  # noqa: N817  ET mirrors stdlib xml.etree convention
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_RELEVANCE_PROMPT = (
    "You are a strict relevance classifier. "
    "Reply with a single token: YES or NO. "
    "A paper is relevant if and only if it could plausibly inform research on: {topic}. "
    "Be conservative: when uncertain, answer NO."
)

DEFAULT_EXTRACTION_PROMPT = (
    "Extract structured fields from the abstract. "
    "Return ONLY a JSON object with these keys: "
    "summary (one sentence), organisms (list[str]), methods (list[str]), "
    "key_findings (list[str]), study_type "
    "(one of: in_silico, in_vitro, in_vivo, clinical, review, other). "
    "If a field is unknown, use an empty string or empty list."
)

# Default topic targeting the qte77 GitHub account's themes (see qte77/qte77
# README: META/KERNEL/MECHANISM authority chain, agentic dev across 30+ repos,
# AI agents-eval blog series). Callers can override via --topic.
DEFAULT_TOPIC = (
    "agentic LLM frameworks, multi-repo orchestration via GitHub Actions, "
    "evaluation methodology for AI coding agents (including Claude Code), "
    "and tool-augmented LLM workflows"
)

RXIV_DETAILS_URL = "https://api.biorxiv.org/details/{server}/{doi}"
ARXIV_QUERY_URL = "https://export.arxiv.org/api/query?id_list={arxiv_id}"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}

StudyType = Literal["in_silico", "in_vitro", "in_vivo", "clinical", "review", "other"]


class Settings(BaseSettings):
    """Process-wide knobs sourced from RXIV_EVAL_* env vars."""

    model_config = SettingsConfigDict(env_prefix="RXIV_EVAL_", case_sensitive=False)
    offline: bool = False
    stub_mode: str = "hash"
    retry_max_attempts: int = 5
    retry_base_secs: float = 4.0
    no_cache: bool = False


class Paper(BaseModel):
    """One row of the producer's weekly CSV. CSV column names are the aliases."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    date: str = Field(alias="Date")
    iso_week: str = Field(alias="ISOWeek")
    doi: str = Field(alias="DOI")
    version: str = Field(alias="Version")
    category: str = Field(alias="Category")
    title: str = Field(alias="Title")
    authors: str = Field(alias="Authors")


class ArxivCsvRow(BaseModel):
    """Raw row of the arxiv producer CSV (`data/arxiv/<year>/<week>.csv`).

    Schema differs from the biorxiv/medrxiv `Paper`: arxiv id replaces DOI,
    no Category or Authors columns, and the title is single-quoted.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    published: str = Field(alias="Published")
    weekday: str = Field(alias="Weekday(Monday==0)")
    updated: str = Field(alias="Updated")
    arxiv_id: str = Field(alias="ID")
    version: str = Field(alias="Version")
    title: str = Field(alias="Title")

    def to_paper(self) -> Paper:
        """Adapt this arxiv row to the normalized `Paper` shape."""
        date_str = self.published[:10]
        iso_week = dt.date.fromisoformat(date_str).isocalendar().week
        return Paper(
            date=date_str,
            iso_week=f"{iso_week:02d}",
            doi=self.arxiv_id,
            version=self.version,
            category="",
            title=self.title.strip().strip("'").strip(),
            authors="",
        )


class Verdict(BaseModel):
    """Per-paper relevance result; also the DOI-cache payload schema."""

    doi: str
    relevant: bool
    raw: str


class ExtractedFields(BaseModel):
    """Structured extraction output.

    `extra='allow'` preserves model-returned unknown keys (e.g. a fallback
    `_raw` blob when parsing fails).
    """

    model_config = ConfigDict(extra="allow")
    summary: str = ""
    organisms: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    study_type: StudyType = "other"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the eval driver."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--feed-repo", required=True)
    p.add_argument("--server", choices=["biorxiv", "medrxiv", "arxiv"], default="biorxiv")
    p.add_argument("--year", default="")
    p.add_argument("--week", default="")
    p.add_argument("--topic", default=DEFAULT_TOPIC)
    p.add_argument("--model", default="openai/gpt-4o-mini")
    p.add_argument("--categories", default="")
    p.add_argument("--max-papers", type=int, default=0)
    p.add_argument("--enrich", action="store_true")
    p.add_argument("--output-dir", default="output")
    return p.parse_args()


def resolve_year_week(year: str, week: str) -> tuple[str, str]:
    """Return the (year, week) pair to fetch; empty inputs default to today (UTC)."""
    if year and week:
        return year, week.zfill(2)
    today = dt.datetime.now(dt.timezone.utc).date()
    iso_year, iso_week, _ = today.isocalendar()
    return year or str(iso_year), week.zfill(2) if week else f"{iso_week:02d}"


def fetch_feed(feed_repo: str, server: str, year: str, week: str, dest: Path) -> None:
    """Download the week's producer CSV via `gh api` and write it to `dest`."""
    path = f"data/{server}/{year}/{week}.csv"
    # S603/S607: list-form invocation (no shell); `gh` is provided by the
    # GitHub Actions runner's PATH. Inputs are workflow-controlled.
    gh_cmd = [
        "gh",
        "api",
        f"repos/{feed_repo}/contents/{path}",
        "-H",
        "Accept: application/vnd.github.raw",
    ]
    proc = subprocess.run(  # noqa: S603
        gh_cmd, check=False, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"Failed to fetch {feed_repo}:{path}\nstderr: {proc.stderr.strip()}"
        )
    dest.write_text(proc.stdout)


def _paper_from_arxiv_row(row: dict) -> Paper:
    """Validate an arxiv producer CSV row and adapt it to `Paper`."""
    return ArxivCsvRow.model_validate(row).to_paper()


def load_papers(csv_path: Path, server: str = "biorxiv") -> list[Paper]:
    """Read the producer CSV and return rows as normalized `Paper` instances."""
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if server == "arxiv":
        return [_paper_from_arxiv_row(row) for row in rows]
    return [Paper.model_validate(row) for row in rows]


def write_papers(papers: list[Paper], dest: Path) -> None:
    """Serialize papers back to a CSV with the canonical column order."""
    fieldnames = ["Date", "ISOWeek", "DOI", "Version", "Category", "Title", "Authors"]
    with dest.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for paper in papers:
            w.writerow(paper.model_dump(by_alias=True))


def _stub_response(user_prompt: str) -> str:
    """Offline stub for tests/local dev. Mode chosen by RXIV_EVAL_STUB_MODE."""
    mode = Settings().stub_mode
    if mode == "yes":
        return "YES"
    if mode == "no":
        return "NO"
    if mode == "flaky" and random.random() < 0.33:  # noqa: S311  fault-injection stub, not crypto
        raise urllib.error.HTTPError(
            url=GITHUB_MODELS_URL,
            code=429,
            msg="simulated rate limit",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
    h = int(hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(), 16)
    return "YES" if h % 4 == 0 else "NO"


def _sanitize_doi(doi: str) -> str:
    return doi.replace("/", "_").replace("\\", "_")


def _cache_path(output_dir: Path, doi: str) -> Path:
    return output_dir / ".cache" / f"{_sanitize_doi(doi)}.json"


def _cache_load(output_dir: Path, doi: str) -> Verdict | None:
    path = _cache_path(output_dir, doi)
    if not path.exists():
        return None
    try:
        return Verdict.model_validate_json(path.read_text())
    except (ValidationError, json.JSONDecodeError):
        return None


def _cache_save(output_dir: Path, verdict: Verdict) -> None:
    path = _cache_path(output_dir, verdict.doi)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(verdict.model_dump_json())


def _attempt(call: Callable[[], str]) -> tuple[str | None, int | None, str | None]:
    """Run ``call`` once.

    Return ``(result, None, None)`` on success, or ``(None, code, msg)`` on
    retryable failure. Non-retryable HTTPErrors raise.
    """
    try:
        return call(), None, None
    except urllib.error.HTTPError as exc:
        if exc.code not in RETRYABLE_HTTP_CODES:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(
                f"GitHub Models HTTP {exc.code}: {detail.strip()}"
            ) from exc
        return None, exc.code, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, None, f"network error: {exc.reason}"


def _with_retry(call: Callable[[], str], settings: Settings) -> str:
    """Run ``call``, retrying on retryable HTTP/URL errors. Reusable across backends."""
    last_code: int | None = None
    last_err: str | None = None
    for attempt in range(settings.retry_max_attempts):
        result, last_code, last_err = _attempt(call)
        if result is not None:
            return result
        if attempt + 1 < settings.retry_max_attempts:
            time.sleep(settings.retry_base_secs * (2 ** attempt))

    code_str = str(last_code) if last_code is not None else "network"
    raise RuntimeError(
        f"GitHub Models {code_str}: gave up after "
        f"{settings.retry_max_attempts} attempts ({last_err})"
    )


def _github_models_call(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    # S310: GITHUB_MODELS_URL is a constant https:// endpoint.
    req = urllib.request.Request(  # noqa: S310
        GITHUB_MODELS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = json.load(resp)
    return body["choices"][0]["message"]["content"]


def gh_models_rest(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    """POST to GitHub Models REST and return the assistant message content.

    Retries 429/5xx and transient network errors with exponential backoff.
    Honors RXIV_EVAL_OFFLINE=1 to skip the network entirely (returns a stub).
    """
    settings = Settings()
    if settings.offline:
        return _with_retry(lambda: _stub_response(user_prompt), settings)
    return _with_retry(
        lambda: _github_models_call(model, system_prompt, user_prompt, max_tokens),
        settings,
    )


def is_relevant(
    paper: Paper,
    abstract: str,
    *,
    model: str,
    system_prompt: str,
    output_dir: Path | None = None,
) -> bool:
    """Return True iff the LLM classifies the paper as relevant to the topic."""
    use_cache = output_dir is not None and not Settings().no_cache
    if use_cache:
        cached = _cache_load(output_dir, paper.doi)  # type: ignore[arg-type]
        if cached is not None:
            return cached.relevant

    user_prompt = (
        f"Title: {paper.title}\n"
        f"Category: {paper.category}\n\n"
        f"Abstract: {abstract or '(unavailable)'}"
    )
    raw = gh_models_rest(model, system_prompt, user_prompt, max_tokens=4)
    relevant = raw.strip().upper().startswith("YES")

    if use_cache:
        _cache_save(
            output_dir,  # type: ignore[arg-type]
            Verdict(doi=paper.doi, relevant=relevant, raw=raw),
        )

    return relevant


def _urlopen_bytes(url: str, timeout: int = 30) -> bytes:
    """Read the body of an HTTPS GET.

    Single chokepoint for outbound HTTP so Bandit B310 is suppressed exactly
    once and callers cannot pass non-https URLs.
    """
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url!r}")
    # Bandit B310 / ruff S310: scheme is enforced above; both linters get the
    # same justification but read different suppression syntaxes.
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310  # noqa: S310
        return resp.read()


def _fetch_arxiv_abstract(arxiv_id: str) -> str:
    url = ARXIV_QUERY_URL.format(arxiv_id=arxiv_id)
    try:
        data = _urlopen_bytes(url)
    except (urllib.error.URLError, TimeoutError) as exc:
        # arxiv API frequently stalls mid-read; the raw ssl/socket layer
        # raises TimeoutError, which is not a URLError subclass.
        print(f"WARN: abstract fetch failed for {arxiv_id}: {exc}", file=sys.stderr)
        return ""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        print(f"WARN: abstract parse failed for {arxiv_id}: {exc}", file=sys.stderr)
        return ""
    summary = root.find("atom:entry/atom:summary", ATOM_NS)
    if summary is None or summary.text is None:
        return ""
    return summary.text.strip()


def _fetch_rxiv_abstract(server: str, doi: str) -> str:
    url = RXIV_DETAILS_URL.format(server=server, doi=doi)
    try:
        payload = json.loads(_urlopen_bytes(url))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"WARN: abstract fetch failed for {doi}: {exc}", file=sys.stderr)
        return ""
    collection = payload.get("collection") or []
    if not collection:
        return ""
    return collection[0].get("abstract", "") or ""


def fetch_abstract(server: str, doi: str) -> str:
    """Dispatch on server and fetch the paper's abstract; empty string on failure."""
    if Settings().offline:
        return ""
    if server == "arxiv":
        return _fetch_arxiv_abstract(doi)
    return _fetch_rxiv_abstract(server, doi)


def extract_fields(abstract: str, *, model: str, system_prompt: str) -> ExtractedFields:
    """Run the structured-extraction prompt and return parsed fields."""
    if not abstract:
        return ExtractedFields()
    raw = gh_models_rest(model, system_prompt, abstract, max_tokens=512)
    try:
        return ExtractedFields.model_validate_json(raw)
    except ValidationError:
        return ExtractedFields.model_validate({"_raw": raw})


def write_summary(
    output_dir: Path,
    *,
    server: str,
    year: str,
    week: str,
    topic: str,
    total: int,
    after_prefilter: int,
    relevant: list[Paper],
) -> None:
    """Render the run's `summary.md` artifact."""
    lines = [
        f"# rxiv eval — {server} {year}-W{week}",
        "",
        f"- Topic: **{topic}**",
        f"- Source rows: {total}",
        f"- After category/cap pre-filter: {after_prefilter}",
        f"- Relevant (LLM YES): {len(relevant)}",
        "",
        "## Relevant papers",
        "",
    ]
    for p in relevant:
        lines.append(f"- [{p.title}](https://doi.org/{p.doi}) — *{p.category}*")
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n")


def _prefilter(papers: list[Paper], categories: str, max_papers: int) -> list[Paper]:
    if categories:
        allow = {c.strip().lower() for c in categories.split(",") if c.strip()}
        papers = [p for p in papers if p.category.lower() in allow]
        print(f"After category filter ({sorted(allow)}): {len(papers)}", file=sys.stderr)
    if max_papers and len(papers) > max_papers:
        papers = papers[:max_papers]
        print(f"Capped to first {max_papers}", file=sys.stderr)
    return papers


def _run_relevance_pass(
    papers: list[Paper],
    *,
    server: str,
    model: str,
    relevance_prompt: str,
    output_dir: Path,
) -> list[tuple[Paper, str]]:
    relevant: list[tuple[Paper, str]] = []
    total = len(papers)
    for i, paper in enumerate(papers, 1):
        abstract = fetch_abstract(server, paper.doi)
        try:
            keep = is_relevant(
                paper,
                abstract,
                model=model,
                system_prompt=relevance_prompt,
                output_dir=output_dir,
            )
        except RuntimeError as exc:
            print(f"WARN: relevance call failed for {paper.doi}: {exc}", file=sys.stderr)
            continue
        marker = "YES" if keep else "no "
        print(f"[{i}/{total}] {marker} {paper.doi} {paper.title[:80]}", file=sys.stderr)
        if keep:
            relevant.append((paper, abstract))
    return relevant


def _run_extraction_pass(
    relevant: list[tuple[Paper, str]],
    *,
    model: str,
    output_dir: Path,
) -> None:
    extraction_prompt = os.environ.get("EXTRACTION_PROMPT") or DEFAULT_EXTRACTION_PROMPT
    with (output_dir / "extracts.jsonl").open("w") as f:
        for paper, abstract in relevant:
            try:
                fields = extract_fields(abstract, model=model, system_prompt=extraction_prompt)
            except RuntimeError as exc:
                print(f"WARN: extract failed for {paper.doi}: {exc}", file=sys.stderr)
                fields = ExtractedFields()
            record = {
                **paper.model_dump(),
                "abstract": abstract,
                "extracted": fields.model_dump(),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    """Drive the full pipeline: fetch, prefilter, classify, enrich, write artifacts."""
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    year, week = resolve_year_week(args.year, args.week)
    (output_dir / ".resolved_yw").write_text(f"{year} {week}\n")

    feed_csv = output_dir / "feed.csv"
    print(f"Fetching {args.feed_repo} -> data/{args.server}/{year}/{week}.csv", file=sys.stderr)
    fetch_feed(args.feed_repo, args.server, year, week, feed_csv)

    papers = load_papers(feed_csv, server=args.server)
    total = len(papers)
    print(f"Loaded {total} papers", file=sys.stderr)

    categories = args.categories
    if args.server == "arxiv" and categories:
        print(
            "WARN: --categories ignored for --server=arxiv "
            "(CSV has no Category column)",
            file=sys.stderr,
        )
        categories = ""

    papers = _prefilter(papers, categories, args.max_papers)
    after_prefilter = len(papers)

    relevance_prompt = (os.environ.get("RELEVANCE_PROMPT") or DEFAULT_RELEVANCE_PROMPT).format(
        topic=args.topic
    )
    relevant = _run_relevance_pass(
        papers,
        server=args.server,
        model=args.model,
        relevance_prompt=relevance_prompt,
        output_dir=output_dir,
    )

    write_papers([p for p, _ in relevant], output_dir / "relevant.csv")

    if args.enrich and relevant:
        _run_extraction_pass(relevant, model=args.model, output_dir=output_dir)

    write_summary(
        output_dir,
        server=args.server,
        year=year,
        week=week,
        topic=args.topic,
        total=total,
        after_prefilter=after_prefilter,
        relevant=[p for p, _ in relevant],
    )

    print(f"Done. Relevant: {len(relevant)}/{after_prefilter}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
