# gha-rxiv-paper-eval

Reusable GitHub Actions workflow that consumes the weekly preprint CSV emitted
by a sibling [`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action)
producer, runs a topic-focused relevance filter through
[GitHub Models](https://docs.github.com/en/github-models), and (optionally)
enriches each hit with the abstract + a structured extraction.

> **Status:** prototype. See [`docs/design.md`](docs/design.md) for the full
> contract, architecture, and roadmap.

## Who this is for

- **Research-group maintainers** who want a weekly digest of bioRxiv /
  medRxiv / arXiv preprints filtered to their lab's topic, dropped into
  GitHub Issues by a downstream triage job — without writing any LLM glue.
- **Multi-repo orgs** running the same eval policy across several consumer
  repos: pin one tag, propagate prompt + model + retry changes by version
  bump rather than copy-paste.
- **Methodology-focused readers** who want methods/findings/study-type
  extracted into structured JSON for downstream pipelines (dashboards,
  spreadsheets, secondary LLM steps).

## Run it locally

The script is runnable standalone via `uv`. The `Makefile` wraps the common
recipes; `make help` lists them all.

```bash
# One-time
make sync                       # uv sync (project + dev)

# Quality gates (matches CI)
make validate                   # lint + complexity + test
make test                       # or individually
make lint
make complexity

# End-to-end smoke against real arxiv (uses your `gh auth token`)
make smoke SERVER=arxiv YEAR=2024 WEEK=24 MAX=5

# Other servers / weeks / output dirs
make smoke SERVER=biorxiv YEAR=2026 WEEK=18 MAX=10 OUT=/tmp/biorxiv-test
```

Outputs land in `$OUT/` (default `/tmp/rxiv-eval/`): `relevant.csv`,
`extracts.jsonl`, `summary.md`, and `feed.csv` (raw producer CSV).

<details>
<summary>Offline smoke (stubbed LLM, no Models call)</summary>

```bash
RXIV_EVAL_OFFLINE=1 RXIV_EVAL_STUB_MODE=yes \
  make smoke SERVER=arxiv YEAR=2024 WEEK=24
```

`RXIV_EVAL_STUB_MODE` accepts `yes`/`no`/`hash`/`flaky` to control the stub's
verdict. Abstracts are sourced from the producer CSV's `Abstract` column;
offline mode skips only the Models REST call.

</details>

<details>
<summary>Raw invocation (no Makefile)</summary>

```bash
GH_TOKEN=$(gh auth token) uv run python scripts/eval_papers.py \
  --feed-repo <owner>/gha-rxiv-feed-action \
  --server biorxiv \
  --topic "<your topic>" \
  --categories "<your categories>" \
  --max-papers 5 \
  --enrich \
  --output-dir /tmp/rxiv-eval
```

</details>

## Use it as a GitHub Actions workflow

In a consumer repo, add a workflow that calls this one. A working in-repo
example is
[`.github/workflows/eval-papers-dispatch.yaml`](.github/workflows/eval-papers-dispatch.yaml)
— the manual dispatch wrapper this repo uses for its own smoke tests. The
job uploads `relevant.csv`, `extracts.jsonl`, and `summary.md` as a build
artifact for downstream jobs.

<details>
<summary>Trigger the dispatch wrapper from CLI</summary>

```bash
# Bare minimum — newest published feed week, biorxiv, max_papers=5 (dispatch default):
gh workflow run eval-papers-dispatch.yaml

# Overrides (any subset):
gh workflow run eval-papers-dispatch.yaml \
  -F server=biorxiv -F year=2026 -F week=20 -F max_papers=50

# Watch + download artifact:
run=$(gh run list --workflow=eval-papers-dispatch.yaml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$run" --exit-status
gh run download "$run"
```

Inputs: `topic`, `server` (biorxiv/medrxiv/arxiv), `year`, `week`,
`categories`, `max_papers` (0 = no cap), `max_llm_calls` (0 = no cap;
caps LLM calls to the top-N papers by topic-keyword overlap), `model`,
`enrich`, `feed_repo`.

</details>

<details>
<summary>Minimum-viable caller</summary>

```yaml
permissions:
  contents: read
  models: read    # lets the auto-provided GITHUB_TOKEN call GitHub Models

jobs:
  eval:
    uses: qte77/gha-rxiv-paper-eval/.github/workflows/eval-papers.yaml@v0.4.0
    with:
      topic: "<your project's relevance criterion>"
      categories: "<comma-separated bioRxiv categories>"
      # eval_ref MUST match the `uses: @<ref>` pin above. A reusable
      # workflow cannot reliably introspect its own ref at runtime.
      eval_ref: v0.4.0
      # eval_repo defaults to qte77/gha-rxiv-paper-eval; fork users set
      # `eval_repo: <their-org>/gha-rxiv-paper-eval`.
      # feed_repo defaults to <caller-owner>/gha-rxiv-feed-action; override if needed.
```

To wire `topic` / `categories` (and similar) from GitHub Actions
repository or organization variables instead of hardcoding, see
[`examples/consumer-eval-vars.yaml`](examples/consumer-eval-vars.yaml).

</details>

<details>
<summary>Downstream triage job (open an issue per relevant paper)</summary>

A second reusable workflow,
[`triage-to-issues.yaml`](.github/workflows/triage-to-issues.yaml), downloads
the eval artifact and opens one GitHub issue per row of `extracts.jsonl`. Add
it as a follow-on job in the same consumer workflow:

```yaml
  triage:
    needs: eval
    if: ${{ fromJSON(needs.eval.outputs.relevant_count) > 0 }}
    uses: qte77/gha-rxiv-paper-eval/.github/workflows/triage-to-issues.yaml@v0.4.0
    with:
      artifact_name: ${{ needs.eval.outputs.artifact_name }}
      # eval_ref MUST match the `uses: @<ref>` pin on this line.
      eval_ref: v0.4.0
      # Optional: label (default "rxiv-feed"), title_prefix (default "rxiv:").
    permissions:
      contents: read
      issues: write
```

The caller needs `issues: write` (granted on the job above) for issue
creation. See [`examples/consumer-eval.yaml`](examples/consumer-eval.yaml)
for a full eval + triage pairing.

</details>

## Auth

The `permissions: models: read` declaration on the caller authorizes the
auto-provided `GITHUB_TOKEN` to call GitHub Models, and covers the public-repo
`gh api` feed fetch performed against the upstream feed-action's data repo.
No additional secret wiring is required for typical weekly batches.

If you ever need to override the token (e.g. to use a fine-grained PAT with
broader scopes), the workflow accepts an optional `models-token` secret and
falls back to `GITHUB_TOKEN` when it's absent.

For local runs the script reads `GH_TOKEN` from the environment — `gh auth
token` provides one with both `gh api` and (depending on your account's
scopes) Models access.

## Docs

- [`docs/design.md`](docs/design.md) — full pipeline + per-server schema adapters.
- [`CHANGELOG.md`](CHANGELOG.md) — what changed.

## Layout

- [`Makefile`](Makefile) — single entry point for local + CI tasks (`make help`).
- [`.github/workflows/eval-papers.yaml`](.github/workflows/eval-papers.yaml) — reusable workflow (`workflow_call`).
- [`.github/workflows/triage-to-issues.yaml`](.github/workflows/triage-to-issues.yaml) — reusable follow-on workflow that opens one GitHub issue per relevant paper.
- [`.github/workflows/eval-papers-dispatch.yaml`](.github/workflows/eval-papers-dispatch.yaml) — manual-dispatch wrapper that doubles as a working example.
- [`scripts/eval_papers.py`](scripts/eval_papers.py) — driver invoked by the workflow; runnable standalone.
- [`scripts/triage_to_issues.py`](scripts/triage_to_issues.py) — driver invoked by the triage workflow; runnable standalone.
- [`docs/design.md`](docs/design.md) — pipeline, inputs/outputs, open questions.
