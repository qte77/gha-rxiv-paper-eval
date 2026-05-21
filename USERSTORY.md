# User Stories

## Primary

**As a GitHub-account owner with a focused technical theme**, I want a
weekly automated digest of new arxiv (or biorxiv/medrxiv) preprints that
are actually relevant to my account's stated interests — so I stop
losing signal across 30+ repos and tens of feeds.

Acceptance:

- Scheduled GitHub Actions workflow runs weekly with no per-run manual
  input required.
- Topic is configurable but ships with a sensible default (qte77 account
  themes for this repo's owner).
- "Relevant" filtering is done by an LLM I control (GitHub Models), not
  a third-party service that holds my taste model.
- Output is machine-readable (`relevant.csv` + `extracts.jsonl`) so I
  can wire it into issue creation, RSS, etc.
- Cost is bounded: I can cap `max_papers` per run and use a small model.

## Secondary

**As a consumer in a different domain** (e.g. a microbiology lab), I
want to call the same reusable workflow from my repo with my own topic
and category allowlist — without forking or editing the eval action.

Acceptance:

- Single `uses:` line in the consumer workflow.
- `feed_repo` defaults to `${caller-owner}/gha-rxiv-feed-action` so an
  org that mirrors both repos gets a working pipeline by default.
- No PAT/secret setup required for GitHub Models — `permissions:
  models: read` on the caller is enough.

## Tertiary

**As a contributor** to this action, I want one entry point for the
common dev tasks — `make sync`, `make test`, `make lint`,
`make complexity`, `make validate`, `make smoke` — so I don't have to
remember the `uv run` invocations.

Acceptance:

- Both CI and the reusable workflow drive their quality gates and dep
  installs through the same Makefile recipes.
- `make help` lists every recipe with a one-line description.

## Non-goals (explicit)

- A general-purpose Anthropic / OpenAI / Ollama adapter. Today the
  inference path is GitHub Models REST. Multi-provider is tracked
  under issue [#12](https://github.com/qte77/gha-rxiv-paper-eval/issues/12).
- Hosting the producer feed CSVs. That responsibility lives in
  [`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action).
- Auto-deriving the topic from a profile README. Manual topic strings
  are sufficient until the eval action proves out for a few weeks.
