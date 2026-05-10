# Design: reusable rxiv eval workflow

This repo ships a single GitHub Actions reusable workflow that consumer repos
call to turn the weekly preprint CSV produced by
[`Lambda-Biolab/gha-rxiv-feed-action`](https://github.com/Lambda-Biolab/gha-rxiv-feed-action)
into a topic-filtered, abstract-enriched feed.

Tracking issue: [Lambda-Biolab/gha-rxiv-feed-action#7](https://github.com/Lambda-Biolab/gha-rxiv-feed-action/issues/7).

## Pipeline

```text
producer CSV  ─►  fetch  ─►  category pre-filter  ─►  abstract fetch  ─►  LLM YES/NO  ─►  LLM extract  ─►  artifact
              gh api         (cheap, optional)        biorxiv API         Models REST   Models REST     upload-artifact
```

1. **Fetch** the week's CSV from `data/<server>/<year>/<week>.csv` in the feed repo.
2. **Category pre-filter (optional, free).** Drop rows whose `Category`
   isn't on the allowlist. Skips paying for LLM calls on obviously off-topic
   work.
3. **Cap (optional).** `max_papers` truncates the candidate set; useful for cost
   ceilings during prototyping.
4. **Abstract fetch.** Pull the abstract from
   `https://api.biorxiv.org/details/{server}/{doi}` for every survivor. The
   abstract is required input for both the relevance and extraction passes
   (titles alone often don't carry enough signal for narrow topics).
5. **Relevance filter.** `POST https://models.github.ai/inference/chat/completions`
   with `temperature=0`, `max_tokens=4`, one paper at a time. The user message
   carries `Title`, `Category`, and `Abstract`. The system prompt is
   `DEFAULT_RELEVANCE_PROMPT` with `{topic}` substituted, or fully overridden
   via the `relevance_prompt` input.
6. **Enrichment (optional).** For each YES, run the extraction prompt against
   the already-fetched abstract.
7. **Artifact upload.** `relevant.csv` + `extracts.jsonl` + `summary.md`.

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
| `model` | `openai/gpt-4o-mini` | Any GitHub Models–supported id. |
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

- **GitHub Models quotas.** Inference is rate / token-limited per GitHub plan.
  For large weeks a worker pool with retries (currently serial) would help.
- **Custom extraction schemas per consumer.** Today the schema is hardcoded in
  `DEFAULT_EXTRACTION_PROMPT`. If schemas diverge a lot, externalize the prompt
  into a per-consumer file the workflow path-inputs.
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

Outputs land in `/tmp/rxiv-eval/`. The script POSTs to the GitHub Models REST
endpoint directly (`https://models.github.ai/inference/chat/completions`); no
`gh` extension is required.
