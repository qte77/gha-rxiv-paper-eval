# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); commits
follow [Conventional Commits](https://www.conventionalcommits.org/). Section
entries are PR-scoped.

## [Unreleased]

### Added
- `examples/consumer-eval.yaml`: drop-in workflow for downstream repos plus a sample triage job.
- Makefile with `sync`/`setup`/`test`/`lint`/`complexity`/`validate`/`smoke`/`help` recipes (#27).
- `Install uv` step + `uv sync --no-dev` in the reusable eval workflow (#23).
- `DEFAULT_TOPIC` targeting the qte77 account themes; `--topic` becomes optional (#21).
- `--server arxiv` support: `ArxivCsvRow` pydantic adapter, Atom XML abstract
  fetcher, manual-dispatch wrapper (#16).

### Changed
- CI workflows pin literal `uv run …` recipes; the Makefile stays for local-dev convenience but is no longer the source of truth for CI gates.
- Bumped `astral-sh/setup-uv` from v6 (Node 20, deprecated) to v8.1.0 (Node 24) (#25).
- GitHub Models calls use the auto-provided `GITHUB_TOKEN` + `permissions: models: read`; the explicit `MODELS_TOKEN` PAT requirement is gone (#24).
- Reusable workflow auto-derives its checkout repo + ref from `github.workflow_ref`/`github.workflow_sha`; `feed_repo` defaults to `${caller-owner}/gha-rxiv-feed-action`; `python_version` is a workflow input (#22).
- Ruff `select` widened to the full strict reference set: `E,F,I,N,W,UP,B,S,SIM,RUF,PT,ANN,TCH,PGH,C90,D` with pydocstyle Google convention (#17, #20).

### Fixed
- `_fetch_arxiv_abstract` catches `TimeoutError` (not a `urllib.error.URLError` subclass); default urlopen timeout 15s → 30s for arxiv API slow paths (#26).
- Runtime-dep install gap: workflow ran the script without `uv sync`, crashing on `import defusedxml` after PR #16 added the dep (#23).

### Security
- `defusedxml.ElementTree` replaces stdlib XML parser for the arxiv Atom response (mitigates billion-laughs / quadratic-blowup; Bandit B314). Single `_urlopen_bytes` chokepoint refuses non-https URLs (Bandit B310). Tracked under the FIXME in `scripts/eval_papers.py`.

## Released history

Pre-`Unreleased` PRs landed without a changelog. Capture summary:

- **#13** refactor: Pydantic + pydantic-settings + pytest + complexipy gate.
- **#9** chore: markdownlint cleanup, lychee config.
- **#8** feat: tier-0 robustness — retry/backoff, offline stub, DOI verdict cache.
- **#4** feat: abstract-aware relevance filter (TDD).
- **#3** docs: LLM provider choice + context-window research.
- **#2** feat: replace `gh models` CLI with GitHub Models REST.
- **#1** feat: initial reusable rxiv paper eval workflow.

Generated using `git log --oneline origin/main`.
