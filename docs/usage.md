# Usage

How to run the eval locally and wire it into a consumer repo. For the
pipeline, inputs/outputs contract and design notes see
[`design.md`](design.md).

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

### Offline smoke (stubbed LLM, no provider call)

```bash
RXIV_EVAL_OFFLINE=1 RXIV_EVAL_STUB_MODE=yes \
  make smoke SERVER=arxiv YEAR=2024 WEEK=24
```

`RXIV_EVAL_STUB_MODE` accepts `yes`/`no`/`hash`/`flaky` to control the stub's
verdict. Abstracts are sourced from the producer CSV's `Abstract` column;
offline mode skips only the LLM REST call.

### Raw invocation (no Makefile)

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

## Use it as a GitHub Actions workflow

In a consumer repo, add a workflow that calls this one. A working in-repo
example is
[`eval-papers-dispatch.yaml`](../.github/workflows/eval-papers-dispatch.yaml)
— the manual dispatch wrapper this repo uses for its own smoke tests. The
job uploads `relevant.csv`, `extracts.jsonl`, and `summary.md` as a build
artifact for downstream jobs.

### Minimum-viable caller

```yaml
permissions:
  contents: read    # `models: read` is no longer needed — GitHub Models is retired

jobs:
  eval:
    uses: qte77/gha-rxiv-paper-eval/.github/workflows/eval-papers.yaml@v0.5.1
    with:
      topic: "<your project's relevance criterion>"
      categories: "<comma-separated bioRxiv categories>"
      # OpenAI-compatible provider (here: Cloudflare Workers AI). See "Auth"
      # below for OpenRouter / Cerebras alternatives.
      api_base: "https://api.cloudflare.com/client/v4/accounts/<account-id>/ai/v1"
      model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
      # eval_ref MUST match the `uses: @<ref>` pin above. A reusable
      # workflow cannot reliably introspect its own ref at runtime.
      eval_ref: v0.5.1
      # eval_repo defaults to qte77/gha-rxiv-paper-eval; fork users set
      # `eval_repo: <their-org>/gha-rxiv-paper-eval`.
      # feed_repo defaults to <caller-owner>/gha-rxiv-feed-action; override if needed.
    secrets:
      # Workers AI API token (create one scoped to "Workers AI").
      llm-api-key: ${{ secrets.CF_WORKERS_AI_TOKEN }}
```

To wire `topic` / `categories` (and similar) from GitHub Actions
repository or organization variables instead of hardcoding, see
[`consumer-eval-vars.yaml`](../examples/consumer-eval-vars.yaml).

### Downstream triage job (open an issue per relevant paper)

A second reusable workflow,
[`triage-to-issues.yaml`](../.github/workflows/triage-to-issues.yaml), downloads
the eval artifact and opens one GitHub issue per row of `extracts.jsonl`. Add
it as a follow-on job in the same consumer workflow:

```yaml
  triage:
    needs: eval
    if: ${{ fromJSON(needs.eval.outputs.relevant_count) > 0 }}
    uses: qte77/gha-rxiv-paper-eval/.github/workflows/triage-to-issues.yaml@v0.5.1
    with:
      artifact_name: ${{ needs.eval.outputs.artifact_name }}
      # eval_ref MUST match the `uses: @<ref>` pin on this line.
      eval_ref: v0.5.1
      # Optional: label (default "rxiv-feed"), title_prefix (default "rxiv:").
    permissions:
      contents: read
      issues: write
```

The caller needs `issues: write` (granted on the job above) for issue
creation. See [`consumer-eval.yaml`](../examples/consumer-eval.yaml)
for a full eval + triage pairing.

### Trigger the dispatch wrapper from CLI

```bash
# Bare minimum — newest published feed week, biorxiv, max_papers=5 (dispatch default):
gh workflow run eval-papers-dispatch.yaml

# Overrides (any subset):
gh workflow run eval-papers-dispatch.yaml \
  -F server=biorxiv -F year=2026 -F week=20 -F max_papers=50

# Watch + download artifact:
run=$(gh run list --workflow=eval-papers-dispatch.yaml --limit 1 \
  --json databaseId --jq '.[0].databaseId')
gh run watch "$run" --exit-status
gh run download "$run"
```

Inputs: `topic`, `server` (biorxiv/medrxiv/arxiv), `year`, `week`,
`categories`, `max_papers` (0 = no cap), `max_llm_calls` (0 = no cap;
caps LLM calls to the top-N papers by topic-keyword overlap), `model`,
`enrich`, `feed_repo`.

## Auth

GitHub Models — this workflow's original LLM provider — was fully retired
on 2026-07-30. `permissions: models: read` no longer does anything; drop it
from the caller (as in the example above).

Point the eval at any OpenAI-compatible chat-completions provider via the
`api_base` input + `llm-api-key` secret:

- **Cloudflare Workers AI** —
  `api_base: https://api.cloudflare.com/client/v4/accounts/<id>/ai/v1`,
  e.g. `model: @cf/meta/llama-3.3-70b-instruct-fp8-fast`.
- **OpenRouter** — `api_base: https://openrouter.ai/api/v1`, any model id it
  serves.
- **Cerebras** — `api_base: https://api.cerebras.ai/v1`, a Cerebras-hosted
  Llama id.

`llm-api-key` falls back to `models-token`/`GITHUB_TOKEN` when unset, which
only works against GitHub Models' now-retired endpoint — set `llm-api-key`
for any live run against a real provider. See
[`llm-providers.md`](llm-providers.md) for the superseded research.

The unrelated `gh api` feed-CSV fetch (against the public feed-action data
repo) still authenticates with the auto-provided `GITHUB_TOKEN`
(`permissions: contents: read` is enough); pass `models-token` only if you
need to override that with a fine-grained PAT.

For local runs the script reads `GH_TOKEN` from the environment — `gh auth
token` provides one for the `gh api` feed fetch.
