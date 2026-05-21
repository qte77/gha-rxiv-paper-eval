# gha-rxiv-paper-eval

Reusable GitHub Actions workflow that consumes the weekly preprint CSV emitted
by a sibling [`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action)
producer, runs a topic-focused relevance filter through
[GitHub Models](https://docs.github.com/en/github-models), and (optionally)
enriches each hit with the abstract + a structured extraction.

> **Status:** prototype. See `docs/design.md` for the contract and open
> questions.

## Usage

In a consumer repo, add a workflow that calls this one. Minimum viable:

```yaml
jobs:
  eval:
    uses: <owner>/gha-rxiv-paper-eval/.github/workflows/eval-papers.yaml@main
    with:
      topic: "<your project's relevance criterion>"
      categories: "<comma-separated bioRxiv categories>"
      # feed_repo defaults to <caller-owner>/gha-rxiv-feed-action; override if needed.
    secrets:
      models-token: ${{ secrets.MODELS_TOKEN }}
```

The job uploads `relevant.csv`, `extracts.jsonl`, and `summary.md` as a
build artifact for downstream jobs (issue creation, indexing, etc).
A copy-pasteable example with a triage job that opens GitHub issues lives at
[`examples/consumer-eval.yaml`](examples/consumer-eval.yaml).

## Required secret

`MODELS_TOKEN` — a token with `models: read`. The default `GITHUB_TOKEN` is
not always enough (org policies, PR-from-fork contexts), so this workflow
takes it as an explicit secret. Create a fine-grained PAT and store it as
the repo or org secret `MODELS_TOKEN`.

## Local smoke test

```bash
GH_TOKEN=$(gh auth token) python scripts/eval_papers.py \
  --feed-repo <owner>/gha-rxiv-feed-action \
  --server biorxiv \
  --topic "<your topic>" \
  --categories "<your categories>" \
  --max-papers 5 \
  --enrich \
  --output-dir /tmp/rxiv-eval
```

No `gh` extension needed — the script POSTs directly to the GitHub Models REST endpoint.

## Layout

- [`.github/workflows/eval-papers.yaml`](.github/workflows/eval-papers.yaml) — reusable workflow (`workflow_call`).
- [`scripts/eval_papers.py`](scripts/eval_papers.py) — driver invoked by the workflow; runnable standalone.
- [`examples/consumer-eval.yaml`](examples/consumer-eval.yaml) — example caller for consumer repos.
- [`docs/design.md`](docs/design.md) — pipeline, inputs/outputs, open questions.
