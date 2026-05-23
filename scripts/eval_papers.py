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
from typing import TYPE_CHECKING, TypeVar

# NOTE: drop defusedxml + Atom parsing once the producer (gha-rxiv-feed-action)
# emits a normalized arxiv CSV that already carries the abstract. See
# docs/design.md "Servers and schema adapters".
import defusedxml.ElementTree as ET  # noqa: N817  ET mirrors stdlib xml.etree convention
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_RELEVANCE_PROMPT = (
    "You are a relevance classifier. Reply with a single token: YES or NO. "
    "A paper is relevant if it could plausibly inform research on: {topic}. "
    "Methodology papers (computational tools, simulation methods, ML / AI "
    "frameworks, structure-guided design pipelines, scaffold-engineering "
    "techniques) count even when their experimental system differs from the "
    "topic's primary targets, provided the methodology is transferable. "
    "When borderline, prefer YES if methodology is transferable."
)

_EXTRACTION_PROMPT_TEMPLATE = (
    "Extract structured fields from the abstract. "
    "Return ONLY a JSON object with these keys: "
    "summary (one sentence), subjects (list[str]: {subjects_hint}), "
    "methods (list[str]), key_findings (list[str]), "
    "study_type (e.g. {study_type_hint}). "
    "If a field is unknown, use an empty string or empty list."
)

# Per-server hints for the generic extraction prompt. The schema is fixed
# (summary/subjects/methods/key_findings/study_type) but the example values
# differ per domain so the model picks domain-appropriate labels.
_EXTRACTION_PROMPT_HINTS: dict[str, dict[str, str]] = {
    "biorxiv": {
        "subjects_hint": "organisms, cell lines, or biological systems",
        "study_type_hint": "in_silico, in_vitro, in_vivo, review",
    },
    "medrxiv": {
        "subjects_hint": "patient cohorts, conditions, or interventions",
        "study_type_hint": "clinical_trial, observational, meta_analysis, review",
    },
    "arxiv": {
        "subjects_hint": "datasets, models, benchmarks, or systems under study",
        "study_type_hint": "theoretical, empirical, system, dataset, survey",
    },
}

DEFAULT_EXTRACTION_PROMPTS: dict[str, str] = {
    server: _EXTRACTION_PROMPT_TEMPLATE.format(**hints)
    for server, hints in _EXTRACTION_PROMPT_HINTS.items()
}

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

# arxiv ids are not DOIs; doi.org won't resolve them. Bio servers use real
# DOIs registered with CrossRef so doi.org is the canonical resolver.
_PAPER_URL_TEMPLATES: dict[str, str] = {
    "arxiv": "https://arxiv.org/abs/{id}",
    "biorxiv": "https://doi.org/{id}",
    "medrxiv": "https://doi.org/{id}",
}


def _paper_url(server: str, paper_id: str) -> str:
    """Return the canonical web URL for a paper given its server and id."""
    template = _PAPER_URL_TEMPLATES.get(server, "https://doi.org/{id}")
    return template.format(id=paper_id)


def _artifact_name(server: str, year: str, week: str) -> str:
    """Build the artifact-upload name for a given (server, year, week) run."""
    return f"rxiv-eval-{server}-{year}-w{week}"


class Settings(BaseSettings):
    """Process-wide knobs sourced from RXIV_EVAL_* env vars."""

    model_config = SettingsConfigDict(env_prefix="RXIV_EVAL_", case_sensitive=False)
    offline: bool = False
    stub_mode: str = "hash"
    retry_max_attempts: int = 5
    retry_base_secs: float = 4.0
    no_cache: bool = False
    # arxiv asks for ~3s between requests; back-to-back fetches otherwise
    # quickly trip HTTP 429. Set to 0 in tests / when running offline.
    arxiv_request_delay_secs: float = 3.0
    # Inter-call gap between LLM relevance requests. GitHub Models free tier
    # is roughly 10-20 req/min; per-call retry can mask short bursts but the
    # honest fix is steady-state throttling. Set to 0 in tests.
    llm_call_interval_secs: float = 1.5
    # OpenAI-compatible chat-completions endpoint. Defaults to GitHub Models;
    # override via RXIV_EVAL_MODELS_URL to point at Azure OpenAI, a local
    # vLLM/Ollama, an OpenAI-compatible proxy, etc. Caller still supplies the
    # bearer token via GH_TOKEN.
    models_url: str = GITHUB_MODELS_URL


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
    no Authors column, title is single-quoted. The producer schema evolved:
    pre-2026 weeks had `Weekday(Monday==0)` and no Categories; 2026+ weeks
    use `ISOWeek` and add a `Categories` column with semicolon-separated
    arxiv tags. Only the always-required fields are validated here; the
    rest are accepted via `extra="ignore"`.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    published: str = Field(alias="Published")
    arxiv_id: str = Field(alias="ID")
    version: str = Field(alias="Version")
    title: str = Field(alias="Title")
    categories: str = Field(default="", alias="Categories")

    def to_paper(self) -> Paper:
        """Adapt this arxiv row to the normalized `Paper` shape."""
        date_str = self.published[:10]
        iso_week = dt.date.fromisoformat(date_str).isocalendar().week
        primary_cat = self.categories.split(";")[0].strip() if self.categories else ""
        return Paper(
            date=date_str,
            iso_week=f"{iso_week:02d}",
            doi=self.arxiv_id,
            version=self.version,
            category=primary_cat,
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
    subjects: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    study_type: str = ""


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


T = TypeVar("T")


def _attempt(call: Callable[[], T]) -> tuple[T | None, int | None, str | None]:
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
                f"HTTP {exc.code}: {detail.strip()}"
            ) from exc
        return None, exc.code, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, None, f"network error: {exc}"


def _with_retry(call: Callable[[], T], settings: Settings) -> T:
    """Run ``call``, retrying on retryable HTTP/URL/timeout errors."""
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
        f"HTTP {code_str}: gave up after "
        f"{settings.retry_max_attempts} attempts ({last_err})"
    )


def _github_models_call(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    payload = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    ).encode("utf-8")
    body = json.loads(
        _urlopen_bytes(
            Settings().models_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
    )
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


def _urlopen_bytes(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    timeout: int = 30,
) -> bytes:
    """Read the body of an HTTPS request (GET or POST).

    Single chokepoint for outbound HTTP so Bandit B310 is suppressed exactly
    once and callers cannot pass non-https URLs. POST callers supply `data`,
    `headers`, and `method="POST"`; GET callers leave those at defaults.
    """
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url!r}")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)  # noqa: S310
    # Bandit B310 / ruff S310: scheme is enforced above; both linters get the
    # same justification but read different suppression syntaxes.
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310  # noqa: S310
        return resp.read()


def _fetch_arxiv_abstract(arxiv_id: str) -> str:
    settings = Settings()
    # arxiv asks for ~3s between requests; back-to-back fetches otherwise
    # quickly trip HTTP 429. Configurable via RXIV_EVAL_ARXIV_REQUEST_DELAY_SECS.
    if settings.arxiv_request_delay_secs > 0:
        time.sleep(settings.arxiv_request_delay_secs)
    url = ARXIV_QUERY_URL.format(arxiv_id=arxiv_id)
    try:
        data = _with_retry(lambda: _urlopen_bytes(url), settings)
    except RuntimeError as exc:
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
    settings = Settings()
    url = RXIV_DETAILS_URL.format(server=server, doi=doi)
    try:
        payload = json.loads(_with_retry(lambda: _urlopen_bytes(url), settings))
    except (RuntimeError, json.JSONDecodeError) as exc:
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


def write_workflow_outputs(
    output_dir: Path, *, relevant_count: int, artifact_name: str
) -> None:
    """Write `key=value` lines for the YAML caller to append to `$GITHUB_OUTPUT`.

    Centralizing the format here removes the inline-shell csv-count and
    artifact-name construction from `eval-papers.yaml`.
    """
    lines = [
        f"relevant_count={relevant_count}",
        f"artifact_name={artifact_name}",
    ]
    (output_dir / ".workflow_outputs").write_text("\n".join(lines) + "\n")


def append_step_summary(
    output_dir: Path, *, server: str, year: str, week: str
) -> None:
    """Mirror `summary.md` into `$GITHUB_STEP_SUMMARY` for inline rendering.

    No-op when the env var is unset or empty (i.e. outside GitHub Actions).
    Appends an artifact-name footer so the zip is discoverable from the
    rendered summary.
    """
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    summary_path = output_dir / "summary.md"
    body = summary_path.read_text() if summary_path.exists() else ""
    footer = f"\n---\n*Artifact: {_artifact_name(server, year, week)}*\n"
    with open(target, "a", encoding="utf-8") as f:
        f.write(body)
        f.write(footer)


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
    call_failures: int = 0,
) -> None:
    """Render the run's `summary.md` artifact.

    `call_failures` is the count of LLM calls that raised after retries
    exhausted; surfacing it is critical because a 100%-rate-limited week
    otherwise looks identical to a true-negative week (issue #6).
    """
    lines = [
        f"# rxiv eval — {server} {year}-W{week}",
        "",
        f"- Topic: **{topic}**",
        f"- Source rows: {total}",
        f"- After category/cap pre-filter: {after_prefilter}",
        f"- Relevant (LLM YES): {len(relevant)}",
    ]
    if after_prefilter > 0:
        pct = round(100 * call_failures / after_prefilter)
        lines.append(f"- LLM call failures: {call_failures} / {after_prefilter} ({pct} %)")
    lines.extend(["", "## Relevant papers", ""])
    for p in relevant:
        lines.append(f"- [{p.title}]({_paper_url(server, p.doi)}) — *{p.category}*")
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
) -> tuple[list[tuple[Paper, str]], int]:
    """Classify papers via the LLM; return (relevant, call_failures).

    `call_failures` counts papers whose `is_relevant` call raised
    `RuntimeError` after exhausting retries — needed so the pipeline can
    distinguish a true-negative week from a rate-limited week (issue #6).
    """
    relevant: list[tuple[Paper, str]] = []
    call_failures = 0
    total = len(papers)
    interval = Settings().llm_call_interval_secs
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
            call_failures += 1
            keep = False
        else:
            marker = "YES" if keep else "no "
            print(f"[{i}/{total}] {marker} {paper.doi} {paper.title[:80]}", file=sys.stderr)
        if keep:
            relevant.append((paper, abstract))
        if i < total and interval > 0:
            time.sleep(interval)
    return relevant, call_failures


def _run_extraction_pass(
    relevant: list[tuple[Paper, str]],
    *,
    server: str,
    model: str,
    output_dir: Path,
) -> None:
    default_prompt = DEFAULT_EXTRACTION_PROMPTS.get(
        server, DEFAULT_EXTRACTION_PROMPTS["biorxiv"]
    )
    extraction_prompt = os.environ.get("EXTRACTION_PROMPT") or default_prompt
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
    relevant, call_failures = _run_relevance_pass(
        papers,
        server=args.server,
        model=args.model,
        relevance_prompt=relevance_prompt,
        output_dir=output_dir,
    )

    write_papers([p for p, _ in relevant], output_dir / "relevant.csv")

    if args.enrich and relevant:
        _run_extraction_pass(
            relevant, server=args.server, model=args.model, output_dir=output_dir
        )

    write_summary(
        output_dir,
        server=args.server,
        year=year,
        week=week,
        topic=args.topic,
        total=total,
        after_prefilter=after_prefilter,
        relevant=[p for p, _ in relevant],
        call_failures=call_failures,
    )

    append_step_summary(output_dir, server=args.server, year=year, week=week)
    write_workflow_outputs(
        output_dir,
        relevant_count=len(relevant),
        artifact_name=_artifact_name(args.server, year, week),
    )

    print(f"Done. Relevant: {len(relevant)}/{after_prefilter}", file=sys.stderr)
    if after_prefilter > 0 and call_failures / after_prefilter > 0.5:
        print(
            f"FAIL: {call_failures}/{after_prefilter} LLM calls failed (>50%); "
            "treating run as broken rather than a true-negative week.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
