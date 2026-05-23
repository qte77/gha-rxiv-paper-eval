# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); commits
follow [Conventional Commits](https://www.conventionalcommits.org/). Section
entries are PR-scoped.

## [Unreleased]

## [0.2.1] - 2026-05-24

### Fixed
- **Critical**: cross-repo callers of `eval-papers.yaml` failed at the
  checkout step in v0.2.0. The auto-derivation of the eval-repo source via
  `github.workflow_ref` / `github.workflow_sha` resolved to the CALLER's
  workflow + commit (per GHA semantics for reusable workflows), so the
  caller's repo was cloned instead of the eval repo's. `actions/checkout`
  then ran `make setup` against the wrong Makefile (no such target),
  failing the run. Restored explicit `eval_repo` (default
  `qte77/gha-rxiv-paper-eval`) and `eval_ref` (required) inputs — caller
  MUST pass `eval_ref` matching its `uses: @<ref>` pin. Self-tests
  in-repo are unaffected; only cross-repo (including fork) callers were
  broken.

### Added
- `eval-papers-dispatch.yaml` becomes the eval-pipeline self-test (#14): runs
  weekly (Tuesday 09:00 UTC) and posts a tracking comment to #14 with the
  artifact name, YES count, run URL, and `summary.md` body. Manual
  dispatches (ad-hoc smokes) run the eval but do not post — keeps #14
  noise-free.

### Security
- Consolidate all `urllib.request.urlopen` calls through `_urlopen_bytes`;
  drops two stray `# noqa: S310` sites. Closes Codacy/Bandit B310 (PR #51).

## [0.2.0] - 2026-05-23

### Added
- Step summary mirroring: `summary.md` is appended to `$GITHUB_STEP_SUMMARY` so per-week verdicts render inline on the workflow run page (closes #37, PR #43).
- LLM call-failure rate surfaced in `summary.md` as `LLM call failures: X / Y (Z %)`, and non-zero exit (code 2) when the rate strictly exceeds 50% — a rate-limited run no longer passes as a true-negative week (closes #6, PR #46).
- Inter-call throttle: `Settings.llm_call_interval_secs` (env `RXIV_EVAL_LLM_CALL_INTERVAL_SECS`, default 1.5s) inserts a steady-state gap between LLM relevance calls (PR #46).
- LLM relevance reasoning surfaced in `summary.md` for both YES and NO verdicts (PR #35).
- Retry + polite inter-request delay for arxiv abstract fetches (PR #34).
- Generic extraction-prompt schema with per-server hint substitution (PR #38).
- Configurable model endpoint via `RXIV_EVAL_MODELS_URL`; defaults to GitHub Models, accepts any OpenAI-compatible chat-completions URL (PR #41).
- CLI dispatch invocation block in `README.md` (PR #49).
- `docs/CHANGELOG.md`, `docs/USERSTORY.md`, `docs/ROADMAP.md`, `docs/ARCHITECTURE.md` (PR #31).
- `examples/consumer-eval.yaml`: drop-in workflow for downstream repos plus a sample triage job.
- Makefile with `sync`/`setup`/`test`/`lint`/`complexity`/`validate`/`smoke`/`help` recipes (PR #27).
- `Install uv` step + `uv sync --no-dev` in the reusable eval workflow (PR #23).
- `DEFAULT_TOPIC` targeting the qte77 account themes; `--topic` becomes optional (PR #21).
- `--server arxiv` support: `ArxivCsvRow` pydantic adapter, Atom XML abstract
  fetcher, manual-dispatch wrapper (PR #16).

### Changed
- Default `DEFAULT_RELEVANCE_PROMPT` loosened: drops "strict" / "if and only if" / "when uncertain, answer NO"; adds methodology clause and tips borderline toward YES when methodology is transferable (closes #5, PR #47). Consumers overriding via `RELEVANCE_PROMPT` env / workflow input are unaffected.
- arxiv 2026 producer schema accepted (drop `Weekday(Monday==0)`, add `Categories`) (PR #32).
- Workflow YAML shrunk: step-summary write, `relevant_count`, and `artifact_name` outputs now flow from Python via `output/.workflow_outputs` (PR #43).
- CI and the reusable eval workflow drive quality gates + dep install via Make recipes (single source of truth for local + CI; PRs #30, #42).
- README leads with local usage and introduces the Makefile recipes (PR #33).
- Bumped `astral-sh/setup-uv` from v6 (Node 20, deprecated) to v8.1.0 (Node 24) (PR #25).
- GitHub Models calls use the auto-provided `GITHUB_TOKEN` + `permissions: models: read`; the explicit `MODELS_TOKEN` PAT requirement is gone (PR #24).
- Reusable workflow auto-derives its checkout repo + ref from `github.workflow_ref`/`github.workflow_sha`; `feed_repo` defaults to `${caller-owner}/gha-rxiv-feed-action`; `python_version` is a workflow input (PR #22).
- Ruff `select` widened to the full strict reference set: `E,F,I,N,W,UP,B,S,SIM,RUF,PT,ANN,TCH,PGH,C90,D` with pydocstyle Google convention (PRs #17, #20).

### Fixed
- arxiv URL resolver uses `https://arxiv.org/abs/{id}` instead of `doi.org` (PR #41).
- `_fetch_arxiv_abstract` catches `TimeoutError` (not a `urllib.error.URLError` subclass); default urlopen timeout 15s → 30s for arxiv API slow paths (PR #26).
- Runtime-dep install gap: workflow ran the script without `uv sync`, crashing on `import defusedxml` after PR #16 added the dep (PR #23).

### Security
- `defusedxml.ElementTree` replaces stdlib XML parser for the arxiv Atom response (mitigates billion-laughs / quadratic-blowup; Bandit B314). Single `_urlopen_bytes` chokepoint refuses non-https URLs (Bandit B310). Tracked under the FIXME in `scripts/eval_papers.py`.

### Notes
- PR #44 dropped `fromJSON(...)` on dispatch inputs based on actionlint advice; it broke runtime workflow-call forwarding and was reverted by PR #48. Net effect on 0.2.0: zero — both omitted intentionally.

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
