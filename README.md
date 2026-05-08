# gha-rxiv-paper-eval

Reusable GitHub Actions workflow that consumes the weekly preprint CSV emitted
by [`Lambda-Biolab/gha-rxiv-feed-action`](https://github.com/Lambda-Biolab/gha-rxiv-feed-action),
runs a topic-focused relevance filter through [GitHub Models](https://github.com/github/gh-models),
and (optionally) enriches each hit with the abstract + a structured extraction.

> **Status:** prototype. See `docs/design.md` for the contract and open
> questions. Tracking issue:
> [`gha-rxiv-feed-action#7`](https://github.com/Lambda-Biolab/gha-rxiv-feed-action/issues/7).

## Usage

In a consumer repo, add a workflow that calls this one. Minimum viable:

```yaml
jobs:
  eval:
    uses: Lambda-Biolab/gha-rxiv-paper-eval/.github/workflows/eval-papers.yaml@main
    with:
      topic: "<your project's relevance criterion>"
      categories: "<comma-separated bioRxiv categories>"
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
  --feed-repo Lambda-Biolab/gha-rxiv-feed-action \
  --server biorxiv \
  --topic "<your topic>" \
  --categories "<your categories>" \
  --max-papers 5 \
  --enrich \
  --output-dir /tmp/rxiv-eval
```

Requires `gh extension install github/gh-models`.

## Layout

- [`.github/workflows/eval-papers.yaml`](.github/workflows/eval-papers.yaml) — reusable workflow (`workflow_call`).
- [`scripts/eval_papers.py`](scripts/eval_papers.py) — driver invoked by the workflow; runnable standalone.
- [`examples/consumer-eval.yaml`](examples/consumer-eval.yaml) — example caller for consumer repos.
- [`docs/design.md`](docs/design.md) — pipeline, inputs/outputs, open questions.
