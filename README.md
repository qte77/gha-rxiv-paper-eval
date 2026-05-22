# gha-rxiv-paper-eval

Reusable GitHub Actions workflow that consumes the weekly preprint CSV emitted
by a sibling [`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action)
producer, runs a topic-focused relevance filter through
[GitHub Models](https://docs.github.com/en/github-models), and (optionally)
enriches each hit with the abstract + a structured extraction.

> **Status:** prototype. See `docs/design.md` for the contract and open
> questions.

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
verdict; abstract fetches return empty in offline mode.

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
<summary>Minimum-viable caller</summary>

```yaml
permissions:
  contents: read
  models: read    # lets the auto-provided GITHUB_TOKEN call GitHub Models

jobs:
  eval:
    uses: <owner>/gha-rxiv-paper-eval/.github/workflows/eval-papers.yaml@main
    with:
      topic: "<your project's relevance criterion>"
      categories: "<comma-separated bioRxiv categories>"
      # feed_repo defaults to <caller-owner>/gha-rxiv-feed-action; override if needed.
```

</details>

<details>
<summary>Downstream triage job (open an issue per relevant paper)</summary>

```yaml
  triage:
    needs: eval
    if: ${{ fromJSON(needs.eval.outputs.relevant_count) > 0 }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v8
        with:
          name: ${{ needs.eval.outputs.artifact_name }}
          path: eval-output
      - env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -euo pipefail
          while IFS= read -r line; do
            doi=$(jq -r '.doi' <<<"$line")
            title=$(jq -r '.title' <<<"$line")
            summary=$(jq -r '.extracted.summary // ""' <<<"$line")
            body=$(jq -n --arg d "$doi" --arg s "$summary" \
              '"DOI: https://doi.org/\($d)\n\n\($s)"')
            gh issue create --title "rxiv: $title" --body "$body" --label "rxiv-feed"
          done < eval-output/extracts.jsonl
```

</details>

## Auth

No secret needed. The `permissions: models: read` declaration on the caller
workflow authorizes the auto-provided `GITHUB_TOKEN` to call GitHub Models;
the same token also covers the public-repo `gh api` feed fetch.

For local runs the script reads `GH_TOKEN` from the environment — `gh auth
token` provides one with both `gh api` and (depending on your account's
scopes) Models access.

## Docs

- [`docs/design.md`](docs/design.md) — full pipeline + per-server schema adapters.
- [`CHANGELOG.md`](CHANGELOG.md) — what changed.

## Layout

- [`Makefile`](Makefile) — single entry point for local + CI tasks (`make help`).
- [`.github/workflows/eval-papers.yaml`](.github/workflows/eval-papers.yaml) — reusable workflow (`workflow_call`).
- [`.github/workflows/eval-papers-dispatch.yaml`](.github/workflows/eval-papers-dispatch.yaml) — manual-dispatch wrapper that doubles as a working example.
- [`scripts/eval_papers.py`](scripts/eval_papers.py) — driver invoked by the workflow; runnable standalone.
- [`docs/design.md`](docs/design.md) — pipeline, inputs/outputs, open questions.
