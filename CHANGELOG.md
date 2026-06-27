# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); commits
follow [Conventional Commits](https://www.conventionalcommits.org/). Section
entries are PR-scoped.

## [Unreleased]

### Added
- **Keyword pre-filter to cap LLM calls (`max_llm_calls`).** New workflow input
  / `--max-llm-calls` CLI flag. After the category/`max_papers` filter, ranks
  surviving papers by topic-keyword overlap and sends only the top N to the
  LLM, so a consumer can stay under a provider's daily quota without picking
  the first N in CSV order. Stdlib-only, deterministic, free; `0` = no cap
  (unchanged behavior). Scoring reads `title + category + abstract` — the
  abstract is the highest-signal field and is CSV-supplied since v0.3.0 (#68).
  Surfaced through both `eval-papers.yaml` and the dispatch wrapper, plus an
  `- After keyword pre-filter: N` line in `summary.md` when the cap fires.
  Closes #7. New helpers `_topic_keywords`, `_keyword_score`, `_keyword_prefilter`.
- **CI release automation.** Three workflows modeled on the sibling `paperverse`
  repo: `bump-my-version` (dispatch a semver bump → opens a release PR),
  `tag-release` (auto-creates the annotated `vX.Y.Z` tag when the bump lands on
  main), and `publish-release` (cuts the GitHub Release from the CHANGELOG
  section). Backed by a `[tool.bumpversion]` config + `bump-my-version` dev dep.
  Closes the gap where v0.2.3–v0.3.0 were changelogged but never tagged/released.

### Fixed
- **Weekly 404 on empty `week`/`year` inputs.** When `week`/`year` are empty,
  the eval now **auto-discovers the newest week the feed has actually
  published** (lists `data/<server>/` via `gh api` and takes the max year/week)
  instead of computing a date-derived default. The feed's publish cadence lags
  the calendar by a variable amount — observed 2 ISO weeks for biorxiv on
  2026-06-27 (feed had W24 while the calendar was W26) — so both the old
  current-week default *and* a naive last-completed (N-1) default 404 before
  any LLM call. Auto-discovery is resilient to any lag and independent of the
  week-start (Mon/Sun) convention. Explicit `week`/`year` still override. New
  helpers `_gh_api_json`, `_max_numeric_entry`, `_discover_latest_week`.
  Closes #69, #61; unblocks the weekly self-test (#14).
- **Dev tools downloaded on every consumer run.** The eval step ran the script
  via `uv run python …`, which re-syncs and pulls the PEP 735 `dev` group
  (`ruff`, `complexipy`, `pytest` + transitives, ~14 MiB) despite `make setup`
  using `uv sync --no-dev`. Pinned the invocation to `uv run --no-dev …` so the
  runtime environment stays runtime-only. Closes #60.

### Docs
- Surfaced `max_llm_calls` / `MAX_LLM_CALLS` across the discoverability
  surfaces: `examples/consumer-eval.yaml`, `examples/consumer-eval-vars.yaml`,
  the `Makefile` `smoke` recipe, `README.md` inputs, and `docs/design.md`
  (pipeline cap step, inputs table, roadmap). #63 (short-acronym matching)
  recorded as the deferred follow-up. Resolves #65.
- Input descriptions (`eval-papers.yaml`, `eval-papers-dispatch.yaml`),
  `README.md`, `docs/design.md`, and both `examples/consumer-eval*.yaml`
  document the empty-`week`/`year` default as the newest published feed week.

## [0.3.0] - 2026-05-31

### Deprecated
- `RXIV_EVAL_ARXIV_REQUEST_DELAY_SECS` env var. Setting it in this release
  emits a `WARN:` line on stderr and is otherwise a no-op (there's no
  remote arxiv fetch left to delay). The check is removed in the next
  release; a one-release deprecation cycle so operators see a hint
  instead of silent ignore.

### Removed
- **Per-paper abstract fetch.** `scripts/eval_papers.py` no longer fetches
  abstracts at runtime — neither the arxiv Atom XML endpoint
  (`export.arxiv.org/api/query`) nor the bio/med JSON endpoint
  (`api.biorxiv.org/details/...`). Abstracts are read directly from the
  producer CSV's new `Abstract` column. Dropped functions:
  `fetch_abstract`, `_fetch_arxiv_abstract`, `_fetch_rxiv_abstract`.
  Removes the 3s × N polite-delay tax on arxiv runs.
- `defusedxml` runtime dependency. Atom XML parsing is gone, so the
  bandit-B314 mitigation has nothing to mitigate. Net dep count: -1.
- `Settings.arxiv_request_delay_secs` and the `RXIV_EVAL_ARXIV_REQUEST_DELAY_SECS`
  env var. Nothing reads either anymore.
- The `--categories` no-op guard for `--server=arxiv` in `main()`. The
  producer's 9-col arxiv schema now ships `Categories`, so the prefilter
  works for all three servers — no need to warn-and-reset.
- `_run_relevance_pass` lost its `server` parameter (only used to dispatch
  fetch_abstract, which is gone).

### Added
- `MIN_FEED_SCHEMA_VERSION = "0.2.2"` constant + `_assert_min_feed_schema`
  header check in `load_papers`. Producer CSVs that lack the `Abstract`
  column now raise `SystemExit` with a clear message pointing the
  operator at the minimum `gha-rxiv-feed-action` release — instead of
  silently shipping empty-abstract verdicts.
- `Paper.abstract` and `ArxivCsvRow.abstract` / `ArxivCsvRow.authors`
  fields (all default to `""` at the model layer; the load-time header
  check enforces presence on real producer CSVs).

### Changed
- `write_papers` canonical column order now includes `Abstract` as the
  trailing column on the bio/med relevant.csv output.
- `extracts.jsonl` records drop the redundant top-level `"abstract"` key
  (paper.model_dump() now contains it via `Paper.abstract`); existing
  consumers reading `record["abstract"]` see no schema change.

### Docs
- `docs/design.md`: pipeline diagram drops the `abstract fetch` stage;
  step 4 reframed as "abstract (CSV-sourced)"; Roadmap "Shipped" gains
  the abstract-in-CSV refactor; the dedicated `defusedxml` rationale
  section is gone; schema table notes both servers ship `Abstract` inline.

## [0.2.4] - 2026-05-31

### Added
- `.github/workflows/triage-to-issues.yaml` — second reusable workflow
  (`workflow_call`) that downloads an eval artifact and opens one GitHub
  issue per row of `extracts.jsonl`. Replaces the inline bash triage
  snippet that consumers were previously expected to copy from
  `README.md` / `examples/consumer-eval.yaml`. Caller grants
  `issues: write` on the triage job; `eval_ref` must match the
  `uses: @<ref>` pin (same constraint as `eval-papers.yaml`).
- `scripts/triage_to_issues.py` — stdlib-only Python driver invoked by
  the new workflow; runnable standalone for local smoke tests against any
  `extracts.jsonl`. Tolerates blank and malformed JSONL lines (warns,
  skips). Auth delegated to `gh` CLI on PATH (uses `GH_TOKEN`).
- `tests/test_triage_to_issues.py` — unit tests for the triage driver
  (title/body composition, malformed-row handling, CLI plumbing).

### Changed
- `examples/consumer-eval.yaml` and `README.md` triage section: replaced
  the inline `while … jq … gh issue create` bash loop with a call to the
  new `triage-to-issues.yaml` reusable workflow. Consumers no longer
  maintain their own triage shell.

### Docs
- `scripts/eval_papers.py:160-161`: `ArxivCsvRow` docstring no longer
  claims "no Authors column" — the 9-col producer schema includes it.

## [0.2.3] - 2026-05-31

### Changed
- `eval-papers.yaml` `secrets.models-token` description and `GH_TOKEN` env
  comment toned down: removed "single-digit-per-day org-wide" and "50-paper
  runs return HTTP 429" claims. PAT is now described as an optional override
  for when quota issues are actually observed. No behavior change.

### Docs
- `docs/design.md`: producer schema updated for both arxiv (9-col with
  `Categories,Authors,Abstract`) and bio/med (8-col with `Authors,Abstract`).
  Prose clarifies that `ArxivCsvRow` already tolerates the new columns and
  that the `--categories` no-op guard is now removable (Categories are
  present in the producer CSV) but not yet removed — tracked as follow-up.
- `docs/design.md`: FIXME status updated to "ready" and scope broadened —
  the Abstract column the FIXME was waiting for is now emitted by the
  producer on all three servers; both per-paper abstract fetches (Atom XML
  for arxiv, JSON for bio/med) remain active until the follow-up refactor
  lands.
- `docs/design.md`: MODELS_TOKEN / PAT framing toned down in "User stories"
  and "Secrets" sections to match README Auth section (PAT is optional
  override, not a requirement for non-trivial batches).
- `docs/design.md`: local smoke test command updated to
  `uv run python scripts/eval_papers.py` (matches README:66; avoids
  missing-dep crashes for new contributors).
- `eval-papers-dispatch.yaml`: fixed stale comment (lines 8-11) that claimed
  the reusable workflow auto-derives repo + ref from
  `github.workflow_ref`/`github.workflow_sha` — this was reverted in v0.2.1;
  the wrapper now explicitly passes `eval_repo`/`eval_ref`.

### Docs (carried from [Unreleased])
- Added `examples/consumer-eval-vars.yaml` showing how to wire `topic` /
  `categories` / `model` / `max_papers` from GitHub Actions repository or
  organization variables instead of hardcoding. Covers the `fromJSON(...)`
  workaround for `max_papers` (typed `number`, but `vars.*` is always
  string) and the per-repo vs org-vars trade-off. README links to it.

## [0.2.2] - 2026-05-24

### Fixed
- **Regression from v0.2.0**: restore optional `models-token` secret on
  `eval-papers.yaml` (`workflow_call.secrets`). v0.2.0 dropped the v0.1.x
  block and hardcoded `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` on the
  assumption that the auto-provided token has enough Models REST quota.
  It does not — the shared org-wide quota is single-digit-per-day and
  any non-trivial weekly batch returns HTTP 429 for every call (a 50-paper
  run on a consumer repo failed 50/50 calls and tripped the >50% abort).
  Workflow now uses `secrets.models-token || secrets.GITHUB_TOKEN`; pass
  a fine-grained PAT for cron use. Pure additive change — callers passing
  no secrets keep the v0.2.1 behavior.

### Changed
- Per-paper log lines now include `(i/N)` progress on both success and
  failure paths in the relevance and extraction passes. Operators tailing
  a long rate-limited run can see position without counting matching lines.

### Docs
- `README.md`: new "Who this is for" section.
- `docs/design.md`: new "User stories" section; "Open prototype questions"
  reorganized into a "Shipped / Open" Roadmap; corrected stale "Secrets:
  None" block and the "auto-derives via `github.workflow_ref`" claim that
  was reverted in v0.2.1.

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
