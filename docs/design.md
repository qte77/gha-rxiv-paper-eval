# Design: reusable rxiv eval workflow

This repo ships two GitHub Actions reusable workflows that consumer repos
chain to turn the weekly preprint CSV produced by a sibling
[`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action)
producer into a topic-filtered, abstract-enriched feed, and (optionally) one
GitHub issue per relevant paper:

- `eval-papers.yaml` — runs the relevance/extraction pipeline and uploads
  `relevant.csv` + `extracts.jsonl` + `summary.md` as an artifact.
- `triage-to-issues.yaml` — downloads that artifact and opens one issue per
  row of `extracts.jsonl` via `scripts/triage_to_issues.py`. Caller grants
  `issues: write`; `eval_ref` must match the `uses: @<ref>` pin.

## User stories

- **As a research-group maintainer**, I want a weekly Issue per relevant
  preprint with title + DOI + extracted methods so I can triage the week's
  feed in five minutes without opening every PDF. Wiring this end-to-end
  is a two-line workflow_call to `triage-to-issues.yaml` (v0.2.4) — no
  bash/jq glue to maintain in the consumer repo.
- **As a multi-repo org owner**, I want a single tagged workflow my teams
  pin to, so a prompt or retry-policy change ships to every consumer by
  version bump (not by copying YAML). Both the eval and triage stages
  ship as reusable workflows pinned by the same `eval_ref`.
- **As an operator watching a long-running scheduled run**, I want to see
  `(i/N)` progress on every per-paper log line so a 50-minute rate-limited
  run is debuggable in real time (v0.2.2).
- **As a downstream pipeline**, I want a stable JSON-schema extraction
  (`summary` / `subjects` / `methods` / `key_findings` / `study_type`) per
  YES paper so a secondary LLM step or dashboard can consume it without
  parsing prose.
- **As a cron consumer**, I want the option to pass a PAT via
  `secrets.models-token` if I hit GitHub-Models quota limits, while the
  auto-provided `GITHUB_TOKEN` covers typical weekly batches without extra
  configuration (v0.2.2 restores the v0.1.x optional secret).
- **As a quota-limited consumer**, I want to cap the run to the top-N
  most-likely-relevant papers (`max_llm_calls`) so a busy week (100–500
  candidates) stays under my daily Models quota — picked by a free,
  deterministic keyword signal rather than the alphabetically-first N that
  `max_papers` would take.

## Pipeline

```text
producer CSV ─► fetch ─► category pre-filter ─► LLM YES/NO ─► LLM extract ─► artifact ─► triage (optional)
             gh api      (cheap, optional)      Models REST  Models REST    upload      gh issue create
```

1. **Fetch** the week's CSV from `data/<server>/<year>/<week>.csv` in the
   feed repo. Header check rejects producer CSVs older than
   `MIN_FEED_SCHEMA_VERSION` (the first feed-action release that ships
   inline abstracts) with a clear error — older outputs would silently
   ship empty-abstract verdicts.
2. **Category pre-filter (optional, free).** Drop rows whose `Category`
   isn't on the allowlist. Skips paying for LLM calls on obviously off-topic
   work.
3. **Cap (optional).** Two independent caps shrink the candidate set before the
   paid LLM step. `max_papers` truncates in CSV order (a cheap cost ceiling for
   prototyping). `max_llm_calls` instead ranks survivors by topic-keyword
   overlap on title+category+abstract and keeps only the top N — picking
   high-signal papers to stay under a provider's daily quota (#7) rather than
   the alphabetically-first N. Both are stdlib-only, deterministic, and free.
4. **Relevance filter.** `POST https://models.github.ai/inference/chat/completions`
   with `temperature=0`, `max_tokens=4`, one paper at a time. The user message
   carries `Title`, `Category`, and `Abstract` (the abstract is sourced from
   the producer CSV's `Abstract` column — no per-paper remote fetch since
   v0.3.0). The system prompt is `DEFAULT_RELEVANCE_PROMPT` with `{topic}`
   substituted, or fully overridden via the `relevance_prompt` input.
5. **Enrichment (optional).** For each YES, run the extraction prompt against
   the same CSV-sourced abstract.
6. **Artifact upload.** `relevant.csv` + `extracts.jsonl` + `summary.md`.
7. **Triage (optional, separate reusable workflow).**
   `triage-to-issues.yaml` (v0.2.4) downloads the artifact and opens one
   GitHub issue per row of `extracts.jsonl` via
   `scripts/triage_to_issues.py` (`gh issue create`, list-form subprocess,
   no shell). Caller grants `issues: write` on the triage job and pins
   `eval_ref` to match the `uses: @<ref>` pin.

## Inputs / outputs

See [`.github/workflows/eval-papers.yaml`](../.github/workflows/eval-papers.yaml)
for the authoritative list. Highlights:

| Input | Default | Notes |
| --- | --- | --- |
| `topic` | required | Free-text. Substituted into the relevance system prompt. |
| `server` | `biorxiv` | `biorxiv`, `medrxiv`, or `arxiv`. |
| `year` / `week` | current ISO | UTC. Override for backfills. |
| `categories` | "" | Comma-separated allowlist. See feed action's `docs/categories.md`. |
| `max_papers` | 0 | 0 = no cap. Truncates in CSV order. |
| `max_llm_calls` | 0 | 0 = no cap. Ranks survivors by topic-keyword overlap (title+category+abstract) and sends only the top N to the LLM — quota-friendly alternative to `max_papers` (#7). |
| `model` | `openai/gpt-4o-mini` | Any GitHub Models–supported id. |
| `enrich` | `true` | Toggles the structured-extraction pass (operates on the CSV-supplied abstract). |
| `relevance_prompt` / `extraction_prompt` | "" | Override the defaults. |
| `eval_repo` | `qte77/gha-rxiv-paper-eval` | Owner/repo hosting the reusable workflow's source. Fork users override. |
| `eval_ref` | **required** | Tag/branch/SHA matching the caller's `uses: @<ref>` pin. Reusable workflows cannot auto-derive this (see v0.2.1 changelog). |

Outputs:

- `relevant_count` (string) — number of papers that passed the filter.
- `artifact_name` (string) — the artifact uploaded by the job, for downstream
  jobs to download.
- `summary.md` is also appended to `$GITHUB_STEP_SUMMARY` so per-week verdicts
  render inline on the run page without downloading the artifact.

Exit codes:

- `0` — normal completion (including zero relevant papers).
- `2` — more than 50% of LLM calls failed after retries exhausted. A rate-
  limited run that produces no real verdicts fails loudly rather than
  silently shipping a fabricated 0-hit result. See `summary.md`'s `LLM call
  failures: X / Y (Z %)` line for the rate.

Environment knobs (`RXIV_EVAL_*`):

- `RETRY_MAX_ATTEMPTS` (default `5`), `RETRY_BASE_SECS` (default `4.0`) —
  exponential backoff for retryable HTTP codes (429, 500, 502, 503, 504).
- `LLM_CALL_INTERVAL_SECS` (default `1.5`) — steady-state gap between
  successive relevance calls. Per-call retry alone cannot rescue a burst that
  trips a per-minute rate ceiling; this throttle prevents the burst.
- `MODELS_URL` — override the chat-completions endpoint (default GitHub
  Models). Accepts any OpenAI-compatible URL.
- `OFFLINE` / `STUB_MODE` — stub the LLM in tests (`yes` / `no` / `hash` /
  `flaky`).

Secrets:

- `models-token` *(optional, since v0.2.2)* — fine-grained PAT with
  `models: read`. The workflow uses
  `secrets.models-token || secrets.GITHUB_TOKEN` for both the `gh api` feed
  fetch and the GitHub Models REST POST. `GITHUB_TOKEN` with
  `permissions: models: read` is sufficient for typical weekly batches;
  supply a fine-grained PAT only if you observe quota-related HTTP 429s in
  practice. Consumers still declare `permissions: models: read` so the
  fallback path works when no PAT is configured.

## Determinism

- `temperature 0` on both passes for reproducibility week-over-week.
- Relevance prompt is constrained to a single token (`YES`/`NO`).
- Extraction prompt asks for a fixed JSON schema; non-JSON responses are
  preserved verbatim under `_raw` for triage rather than dropped.

## Servers and schema adapters

| Server | Producer CSV columns |
| --- | --- |
| `biorxiv`, `medrxiv` | `Date,ISOWeek,DOI,Version,Category,Title,Authors,Abstract` |
| `arxiv` | `Published,ISOWeek,Updated,ID,Version,Title,Categories,Authors,Abstract` |

Both shapes ship the `Abstract` inline as of `gha-rxiv-feed-action`
v0.2.2 (= `MIN_FEED_SCHEMA_VERSION`); the eval script reads it directly
from the row. Older producer outputs that lack the column are rejected at
`load_papers` with a `SystemExit` pointing the operator at the minimum
feed-action release.

The `ArxivCsvRow` pydantic adapter maps `Categories` (semicolon-separated)
to `Paper.category` via `to_paper()` and passes `Authors` + `Abstract`
through unchanged. Both Authors and Abstract default to `""` at the row
model layer so older pre-9-col fixtures still validate; the load-time
header check is what enforces the production minimum.

### Outbound HTTP chokepoint

The remaining outbound non-Models call is `gh api` for the feed-CSV
fetch. The chokepoint `_urlopen_bytes(url, timeout=30)` is still defined
for the Models REST POST and refuses non-`https://` schemes — single
Bandit B310 suppression site.

## Why a separate eval repo (vs. living in the feed repo)

- **Separation of concerns.** The feed action's job is to emit the CSV; eval
  policy (topic, categories, model) is per-consumer and changes more often
  than the producer.
- **Centralized prompt evolution.** Tweaks to the relevance/extraction prompt
  propagate to every consumer by tag.
- **Versioning.** Consumers pin the workflow's `uses:` ref to a tag, so the
  prompt contract is stable across runs until they explicitly bump.
  Callers MUST pass `eval_ref` matching the `uses: @<ref>` pin so the
  script version aligns with the workflow version (reusable workflows
  cannot auto-derive this — `github.workflow_ref` /
  `github.workflow_sha` resolve to the CALLER, see v0.2.1 changelog).

## Roadmap

### Shipped

- **GitHub Models quotas.** Inference is rate / token-limited per GitHub plan.
  0.2.0 adds steady-state throttling (`RXIV_EVAL_LLM_CALL_INTERVAL_SECS`) and
  exponential backoff on 429/5xx, plus a >50%-failure-rate hard exit so
  rate-limited runs are visible. 0.2.2 restores the optional `models-token`
  PAT pass-through so cron consumers can step around `GITHUB_TOKEN`'s tiny
  shared org quota.
- **In-run observability.** 0.2.2 surfaces `(i/N)` progress on every
  per-paper log line (success + failure, relevance + extraction passes), so
  long rate-limited runs are debuggable from the streaming log.
- **DOI cache.** A keyed per-DOI verdict cache lives under
  `<output-dir>/.cache/` (`Verdict` JSON per sanitized DOI). Disable via
  `RXIV_EVAL_NO_CACHE=1`. Same paper across two weeks pays once.
- **Triage as a reusable workflow.** 0.2.4 promotes the inline
  bash/jq/`gh issue create` triage loop (previously copy-pasted by
  consumers from the README example) into a second reusable workflow,
  `triage-to-issues.yaml`, backed by `scripts/triage_to_issues.py`.
  Consumers chain it after the eval job with a two-line `uses:`. Same
  `eval_ref` pinning constraint as `eval-papers.yaml`.
- **Abstract sourced from producer CSV.** 0.3.0 drops the per-paper
  arxiv Atom XML + biorxiv JSON abstract fetches (and the `defusedxml`
  runtime dep) in favour of reading the `Abstract` column emitted by
  `gha-rxiv-feed-action` v0.2.2+. Eliminates the 3s × N polite-delay
  tax on arxiv runs and removes two retry-prone HTTP code paths.
  `load_papers` enforces `MIN_FEED_SCHEMA_VERSION` so a stale producer
  pin fails loudly instead of shipping empty-abstract verdicts.
- **Keyword pre-filter to cap LLM calls** (#7). `max_llm_calls` ranks the
  post-category candidate set by topic-keyword overlap on
  title+category+abstract and sends only the top N to the LLM, so a consumer
  can stay under a provider's daily quota without picking the first N in CSV
  order. Stdlib-only, deterministic, free; `0` = no cap (unchanged behavior).

### Open

- **Short-acronym keyword matching** (#63). The `_topic_keywords` tokenizer
  drops tokens ≤ 3 chars, so domain acronyms (AMP, RNA, LLM) named in the
  topic don't count toward the overlap score. Deferred until a real weekly
  run misranks on a dropped acronym.
- **Custom extraction schemas per consumer.** Today the schema is hardcoded
  in `DEFAULT_EXTRACTION_PROMPT`. If schemas diverge a lot, externalize the
  prompt into a per-consumer file the workflow path-inputs.
- **Cross-consumer DOI cache.** The current cache is per-output-dir.
  Sharing a verdict cache across consumer repos (same DOI evaluated by
  multiple downstream orgs) would need a network-addressable store
  (artifact-as-cache, gist, or repo-pinned JSON).

## Local smoke test

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

Outputs land in `/tmp/rxiv-eval/`. The script POSTs to the GitHub Models REST
endpoint directly (`https://models.github.ai/inference/chat/completions`); no
`gh` extension is required.
