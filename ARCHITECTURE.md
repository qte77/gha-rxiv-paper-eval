# Architecture

High-level overview. For pipeline details, schema adapters, and the
defusedxml/Bandit suppressions, see [`docs/design.md`](docs/design.md).

## Components

```text
caller workflow            this repo (reusable)              feed repo
────────────────           ─────────────────────             ─────────────────────
.github/workflows/         .github/workflows/                qte77/gha-rxiv-feed-action
  rxiv-eval.yaml           ▸ eval-papers.yaml  ◀──uses──── .github/workflows/...
                           ▸ eval-papers-dispatch.yaml      data/<server>/<y>/<w>.csv
                           ▸ ci.yaml
                           scripts/
                           ▸ eval_papers.py
                           docs/design.md
                           Makefile
```

## Modules

| Path | Role |
| --- | --- |
| `.github/workflows/eval-papers.yaml` | Reusable workflow (`workflow_call`). Consumer repos invoke it via `uses:`. Auto-derives its own checkout repo + sha from `github.workflow_ref`. |
| `.github/workflows/eval-papers-dispatch.yaml` | Thin `workflow_dispatch` wrapper exposing the reusable workflow to the Actions UI for smoke tests. |
| `.github/workflows/ci.yaml` | Per-PR quality gates (lint / complexity / tests) via `make`. |
| `scripts/eval_papers.py` | Driver. Fetches CSV → optional category prefilter → per-row LLM relevance pass → optional structured extraction → writes `relevant.csv` + `extracts.jsonl` + `summary.md`. Runnable standalone. |
| `Makefile` | Single entry point for local + CI. Recipes: `sync`, `setup`, `test`, `lint`, `complexity`, `validate`, `smoke`. |
| `tests/` | pytest. `test_models.py` covers pydantic schemas; `test_eval_papers.py` covers retry/cache/offline/timeout paths + integration via `main()`. |
| `pyproject.toml` | Runtime deps (defusedxml, pydantic, pydantic-settings) + dev deps + ruff strict ruleset + complexipy cognitive-complexity gate (max=10). |

## Data flow

1. Caller pins `uses: <owner>/gha-rxiv-paper-eval/.github/workflows/eval-papers.yaml@<ref>` with `permissions: models: read`.
2. Reusable workflow resolves `repo + sha` from `github.workflow_ref`, checks itself out, sets up Python + uv, runs `make setup`.
3. `scripts/eval_papers.py`:
   - `gh api repos/<feed_repo>/contents/data/<server>/<year>/<week>.csv` → loads rows.
   - Category prefilter (skipped for arxiv since the producer CSV omits Category).
   - `max_papers` cap.
   - Per row: fetch abstract (biorxiv JSON or arxiv Atom XML via defusedxml) → POST to GitHub Models REST with the relevance prompt.
   - For each YES: a second Models POST with the extraction prompt.
   - Write artifacts.
4. Upload-artifact step makes `relevant.csv` + `extracts.jsonl` + `summary.md` available to downstream jobs in the caller workflow.

## Trust boundaries

- **Outbound HTTP** funnels through `_urlopen_bytes`, which refuses non-`https://` schemes. One Bandit B310 suppression sits there.
- **XML parsing** uses `defusedxml.ElementTree` (B314). Removable once the producer normalizes the CSV (see `docs/design.md` "FIXME — XML dependency removable once producer normalizes").
- **Auth** uses the auto-provided `GITHUB_TOKEN` plus `permissions: models: read`. No external PATs.

## Determinism + cost controls

- `temperature 0` on both LLM passes.
- Relevance is constrained to a single token (`YES`/`NO`); `max_tokens=4`.
- A DOI-keyed cache short-circuits the relevance call on re-runs.
- `max_papers` caps cost during smoke tests.

## See also

- [`USERSTORY.md`](USERSTORY.md) — who this is for.
- [`ROADMAP.md`](ROADMAP.md) — what's next.
- [`CHANGELOG.md`](CHANGELOG.md) — what changed.
- [`docs/design.md`](docs/design.md) — full pipeline / I-O / open questions / per-server adapter rationale.
