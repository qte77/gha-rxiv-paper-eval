# gha-rxiv-paper-eval

> Reusable GitHub Actions workflow that filters a weekly bioRxiv / medRxiv /
> arXiv preprint feed to your topic with an LLM and extracts structured
> findings from each hit. Prototype.

[![Version](https://img.shields.io/badge/version-v0.6.0-blue)](CHANGELOG.md)
[![CodeFactor](https://www.codefactor.io/repository/github/qte77/gha-rxiv-paper-eval/badge)](https://www.codefactor.io/repository/github/qte77/gha-rxiv-paper-eval)
[![CI](https://github.com/qte77/gha-rxiv-paper-eval/actions/workflows/ci.yaml/badge.svg)](https://github.com/qte77/gha-rxiv-paper-eval/actions/workflows/ci.yaml)

## What

- Reads the weekly CSV published by
  [`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action) —
  no scraping, no per-paper fetches.
- Judges each paper against a free-text topic with any OpenAI-compatible LLM
  provider (Cloudflare Workers AI, OpenRouter, Cerebras, …).
- Optional: route that judgment through the shared `decide()` calibrated-
  confidence gate first, falling back to the chat call for its uncertain
  middle band.
- Optional category allowlist and keyword-ranked LLM-call cap keep runs inside
  provider quotas.
- Extracts methods / findings / study type of each hit into `extracts.jsonl`
  for dashboards, spreadsheets or a second LLM step.
- Uploads `relevant.csv`, `extracts.jsonl` and `summary.md` as a build artifact.
- Optional follow-on workflow opens one GitHub issue per relevant paper.

## How

Call the reusable workflow from a consumer repo:

```yaml
jobs:
  eval:
    uses: qte77/gha-rxiv-paper-eval/.github/workflows/eval-papers.yaml@v0.6.0
    with:
      topic: "<your project's relevance criterion>"
      api_base: "https://api.cloudflare.com/client/v4/accounts/<account-id>/ai/v1"
      model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
      eval_ref: v0.6.0  # must match the `uses: @<ref>` pin
    secrets:
      llm-api-key: ${{ secrets.CF_WORKERS_AI_TOKEN }}
```

Local runs, the triage job, provider auth and the dispatch CLI are in
[`docs/usage.md`](docs/usage.md).

## Why

Weekly preprint digests usually mean hand-written LLM glue copied into every
repo that wants one. This is one reusable workflow instead: consumer repos pin
a tag and pick up prompt, model and retry changes with a version bump, not a
copy-paste.

## Refs

- [`docs/usage.md`](docs/usage.md)
- [`docs/design.md`](docs/design.md)
- [`docs/llm-providers.md`](docs/llm-providers.md)
- [Decision-model conventions][decide-conventions] — when the `decide()`
  gate is used, defaults, shadow-first rollout, secret names
- [`examples/`](examples/)
- [`CHANGELOG.md`](CHANGELOG.md)
- [`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action)

[decide-conventions]: https://github.com/qte77/.github/blob/main/actions/decide/CONVENTIONS.md
