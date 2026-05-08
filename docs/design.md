# Design: reusable rxiv eval workflow

This repo ships a single GitHub Actions reusable workflow that consumer repos
call to turn the weekly preprint CSV produced by
[`Lambda-Biolab/gha-rxiv-feed-action`](https://github.com/Lambda-Biolab/gha-rxiv-feed-action)
into a topic-filtered, abstract-enriched feed.

Tracking issue: [Lambda-Biolab/gha-rxiv-feed-action#7](https://github.com/Lambda-Biolab/gha-rxiv-feed-action/issues/7).

## Pipeline

```
producer CSV  ─►  fetch  ─►  category pre-filter  ─►  LLM YES/NO  ─►  abstract fetch  ─►  LLM extract  ─►  artifact
              gh api         (cheap, optional)        gh-models      biorxiv API         gh-models       upload-artifact
```

1. **Fetch** the week's CSV from `data/<server>/<year>/<week>.csv` in the feed repo.
2. **Category pre-filter (optional, free).** Drop rows whose `Category`
   isn't on the allowlist. Skips paying for LLM calls on obviously off-topic
   work.
3. **Cap (optional).** `max_papers` truncates the candidate set; useful for cost
   ceilings during prototyping.
4. **Relevance filter.** `gh models run --temperature 0 --max-tokens 4` per row;
   the system prompt is `DEFAULT_RELEVANCE_PROMPT` with `{topic}` substituted,
   or fully overridden via the `relevance_prompt` input.
5. **Enrichment (optional).** For each YES, fetch the abstract from
   `https://api.biorxiv.org/details/{server}/{doi}` and run an extraction
   prompt that returns JSON.
6. **Artifact upload.** `relevant.csv` + `extracts.jsonl` + `summary.md`.

## Inputs / outputs

See [`.github/workflows/eval-papers.yaml`](../.github/workflows/eval-papers.yaml)
for the authoritative list. Highlights:

| Input | Default | Notes |
| --- | --- | --- |
| `topic` | required | Free-text. Substituted into the relevance system prompt. |
| `server` | `biorxiv` | `biorxiv` or `medrxiv`. |
| `year` / `week` | current ISO | UTC. Override for backfills. |
| `categories` | "" | Comma-separated allowlist. See feed action's `docs/categories.md`. |
| `max_papers` | 0 | 0 = no cap. |
| `model` | `openai/gpt-4o-mini` | Any `gh models`-supported id. |
| `enrich` | `true` | Toggles the abstract fetch + extraction pass. |
| `relevance_prompt` / `extraction_prompt` | "" | Override the defaults. |

Outputs:

- `relevant_count` (string) — number of papers that passed the filter.
- `artifact_name` (string) — the artifact uploaded by the job, for downstream
  jobs to download.

Secrets:

- `models-token` (required) — a token with `models: read` scope. Provider:
  GitHub Models. The default `GITHUB_TOKEN` is **not** sufficient for some
  org policies / fork PR contexts, so the workflow forces consumers to pass
  one explicitly. Store as `MODELS_TOKEN` in the consumer repo's secrets and
  forward via `secrets: models-token: ${{ secrets.MODELS_TOKEN }}` on the
  `uses:` block.

## Determinism

- `temperature 0` on both passes for reproducibility week-over-week.
- Relevance prompt is constrained to a single token (`YES`/`NO`).
- Extraction prompt asks for a fixed JSON schema; non-JSON responses are
  preserved verbatim under `_raw` for triage rather than dropped.

## Why a separate eval repo (vs. living in the feed repo)

- **Separation of concerns.** The feed action's job is to emit the CSV; eval
  policy (topic, categories, model) is per-consumer and changes more often
  than the producer.
- **Centralized prompt evolution.** Tweaks to the relevance/extraction prompt
  propagate to every consumer by tag.
- **Versioning.** Consumers pin `eval_repo_ref` to a tag, so the prompt
  contract is stable across runs until they explicitly bump.

## Open prototype questions

- **`gh models` quotas.** GitHub Models has rate / token quotas tied to the
  caller's GitHub plan. For large weeks a worker pool with retries (currently
  serial) would help.
- **Custom extraction schemas per consumer.** Today the schema is hardcoded in
  `DEFAULT_EXTRACTION_PROMPT`. If schemas diverge a lot, switch to gh-models'
  prompt-file (`.prompt.yml`) feature with per-consumer files.
- **Caching.** Same DOI evaluated across two consumers pays twice. A keyed
  cache (DOI → relevance verdict + extract) would be cheap to add later if it
  matters.

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

Outputs land in `/tmp/rxiv-eval/`. Requires `gh extension install github/gh-models`.
