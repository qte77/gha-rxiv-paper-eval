#!/usr/bin/env python3
"""Evaluate a weekly preprint CSV via the GitHub Models REST API.

Pipeline:
    1. Fetch data/<server>/<year>/<week>.csv from the feed repo.
    2. Optional cheap pre-filter on category allowlist + max_papers cap.
    3. LLM relevance pass (YES/NO, temperature 0) per row -> relevant.csv.
    4. Optional enrichment: fetch abstract from rxiv API + structured extraction
       -> extracts.jsonl.
    5. Write summary.md.

Designed to be invoked from .github/workflows/eval-papers.yaml. Stdlib only.
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
from dataclasses import asdict, dataclass
from pathlib import Path

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
    "key_findings (list[str]), study_type (one of: in_silico, in_vitro, in_vivo, clinical, review, other). "
    "If a field is unknown, use an empty string or empty list."
)

RXIV_DETAILS_URL = "https://api.biorxiv.org/details/{server}/{doi}"


@dataclass
class Paper:
    date: str
    iso_week: str
    doi: str
    version: str
    category: str
    title: str
    authors: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Paper":
        return cls(
            date=row["Date"],
            iso_week=row["ISOWeek"],
            doi=row["DOI"],
            version=row["Version"],
            category=row["Category"],
            title=row["Title"],
            authors=row["Authors"],
        )

    def as_row(self) -> dict[str, str]:
        return {
            "Date": self.date,
            "ISOWeek": self.iso_week,
            "DOI": self.doi,
            "Version": self.version,
            "Category": self.category,
            "Title": self.title,
            "Authors": self.authors,
        }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--feed-repo", required=True)
    p.add_argument("--server", choices=["biorxiv", "medrxiv"], default="biorxiv")
    p.add_argument("--year", default="")
    p.add_argument("--week", default="")
    p.add_argument("--topic", required=True)
    p.add_argument("--model", default="openai/gpt-4o-mini")
    p.add_argument("--categories", default="")
    p.add_argument("--max-papers", type=int, default=0)
    p.add_argument("--enrich", action="store_true")
    p.add_argument("--output-dir", default="output")
    return p.parse_args()


def resolve_year_week(year: str, week: str) -> tuple[str, str]:
    if year and week:
        return year, week.zfill(2)
    today = dt.datetime.now(dt.timezone.utc).date()
    iso_year, iso_week, _ = today.isocalendar()
    return year or str(iso_year), week.zfill(2) if week else f"{iso_week:02d}"


def fetch_feed(feed_repo: str, server: str, year: str, week: str, dest: Path) -> None:
    path = f"data/{server}/{year}/{week}.csv"
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{feed_repo}/contents/{path}",
            "-H",
            "Accept: application/vnd.github.raw",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"Failed to fetch {feed_repo}:{path}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    dest.write_text(proc.stdout)


def load_papers(csv_path: Path) -> list[Paper]:
    with csv_path.open(newline="") as f:
        return [Paper.from_row(row) for row in csv.DictReader(f)]


def write_papers(papers: list[Paper], dest: Path) -> None:
    fieldnames = ["Date", "ISOWeek", "DOI", "Version", "Category", "Title", "Authors"]
    with dest.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for paper in papers:
            w.writerow(paper.as_row())


GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"

RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def _stub_response(user_prompt: str) -> str:
    """Offline stub for tests/local dev. Mode chosen by RXIV_EVAL_STUB_MODE."""
    mode = os.environ.get("RXIV_EVAL_STUB_MODE", "hash")
    if mode == "yes":
        return "YES"
    if mode == "no":
        return "NO"
    if mode == "flaky" and random.random() < 0.33:
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


def _cache_load(output_dir: Path, doi: str) -> dict | None:
    path = _cache_path(output_dir, doi)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _cache_save(output_dir: Path, doi: str, payload: dict) -> None:
    path = _cache_path(output_dir, doi)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False))


def gh_models_rest(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    """POST to GitHub Models REST and return the assistant message content.

    Retries 429/5xx and transient network errors with exponential backoff.
    Honors RXIV_EVAL_OFFLINE=1 to skip the network entirely (returns a stub).
    """
    max_attempts = int(os.environ.get("RXIV_EVAL_RETRY_MAX_ATTEMPTS", "5"))
    base = float(os.environ.get("RXIV_EVAL_RETRY_BASE_SECS", "4.0"))
    offline = os.environ.get("RXIV_EVAL_OFFLINE") == "1"

    def _do_call() -> str:
        if offline:
            return _stub_response(user_prompt)
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        req = urllib.request.Request(
            GITHUB_MODELS_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        return body["choices"][0]["message"]["content"]

    last_code: int | None = None
    last_err: str | None = None

    for attempt in range(max_attempts):
        try:
            return _do_call()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_CODES:
                detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                raise RuntimeError(
                    f"GitHub Models HTTP {exc.code}: {detail.strip()}"
                ) from exc
            last_code = exc.code
            last_err = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            last_err = f"network error: {exc.reason}"

        if attempt + 1 < max_attempts:
            time.sleep(base * (2 ** attempt))

    code_str = str(last_code) if last_code is not None else "network"
    raise RuntimeError(
        f"GitHub Models {code_str}: gave up after {max_attempts} attempts ({last_err})"
    )


def is_relevant(
    paper: Paper,
    abstract: str,
    *,
    model: str,
    system_prompt: str,
    output_dir: Path | None = None,
) -> bool:
    cache_dir: Path | None = output_dir
    if os.environ.get("RXIV_EVAL_NO_CACHE") == "1":
        cache_dir = None

    if cache_dir is not None:
        cached = _cache_load(cache_dir, paper.doi)
        if cached is not None:
            return bool(cached.get("relevant"))

    user_prompt = (
        f"Title: {paper.title}\n"
        f"Category: {paper.category}\n\n"
        f"Abstract: {abstract or '(unavailable)'}"
    )
    verdict = gh_models_rest(model, system_prompt, user_prompt, max_tokens=4)
    relevant = verdict.strip().upper().startswith("YES")

    if cache_dir is not None:
        _cache_save(
            cache_dir,
            paper.doi,
            {"doi": paper.doi, "relevant": relevant, "raw": verdict},
        )

    return relevant


def fetch_abstract(server: str, doi: str) -> str:
    if os.environ.get("RXIV_EVAL_OFFLINE") == "1":
        return ""
    url = RXIV_DETAILS_URL.format(server=server, doi=doi)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"WARN: abstract fetch failed for {doi}: {exc}", file=sys.stderr)
        return ""
    collection = payload.get("collection") or []
    if not collection:
        return ""
    return collection[0].get("abstract", "") or ""


def extract_fields(abstract: str, *, model: str, system_prompt: str) -> dict:
    if not abstract:
        return {}
    raw = gh_models_rest(model, system_prompt, abstract, max_tokens=512)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Tolerate prose responses; surface the raw payload for debugging.
        return {"_raw": raw}


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


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    year, week = resolve_year_week(args.year, args.week)
    (output_dir / ".resolved_yw").write_text(f"{year} {week}\n")

    feed_csv = output_dir / "feed.csv"
    print(f"Fetching {args.feed_repo} -> data/{args.server}/{year}/{week}.csv", file=sys.stderr)
    fetch_feed(args.feed_repo, args.server, year, week, feed_csv)

    papers = load_papers(feed_csv)
    total = len(papers)
    print(f"Loaded {total} papers", file=sys.stderr)

    if args.categories:
        allow = {c.strip().lower() for c in args.categories.split(",") if c.strip()}
        papers = [p for p in papers if p.category.lower() in allow]
        print(f"After category filter ({sorted(allow)}): {len(papers)}", file=sys.stderr)

    if args.max_papers and len(papers) > args.max_papers:
        papers = papers[: args.max_papers]
        print(f"Capped to first {args.max_papers}", file=sys.stderr)

    after_prefilter = len(papers)

    relevance_prompt = (os.environ.get("RELEVANCE_PROMPT") or DEFAULT_RELEVANCE_PROMPT).format(
        topic=args.topic
    )

    relevant: list[tuple[Paper, str]] = []
    for i, paper in enumerate(papers, 1):
        abstract = fetch_abstract(args.server, paper.doi)
        try:
            keep = is_relevant(
                paper,
                abstract,
                model=args.model,
                system_prompt=relevance_prompt,
                output_dir=output_dir,
            )
        except RuntimeError as exc:
            print(f"WARN: relevance call failed for {paper.doi}: {exc}", file=sys.stderr)
            continue
        marker = "YES" if keep else "no "
        print(f"[{i}/{after_prefilter}] {marker} {paper.doi} {paper.title[:80]}", file=sys.stderr)
        if keep:
            relevant.append((paper, abstract))

    write_papers([p for p, _ in relevant], output_dir / "relevant.csv")

    if args.enrich and relevant:
        extraction_prompt = os.environ.get("EXTRACTION_PROMPT") or DEFAULT_EXTRACTION_PROMPT
        with (output_dir / "extracts.jsonl").open("w") as f:
            for paper, abstract in relevant:
                try:
                    fields = extract_fields(
                        abstract, model=args.model, system_prompt=extraction_prompt
                    )
                except RuntimeError as exc:
                    print(f"WARN: extract failed for {paper.doi}: {exc}", file=sys.stderr)
                    fields = {}
                record = {**asdict(paper), "abstract": abstract, "extracted": fields}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

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
