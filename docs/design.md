# Design: reusable rxiv eval workflow

This repo ships a single GitHub Actions reusable workflow that consumer repos
call to turn the weekly preprint CSV produced by a sibling
[`gha-rxiv-feed-action`](https://github.com/qte77/gha-rxiv-feed-action)
producer into a topic-filtered, abstract-enriched feed.

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
| `server` | `biorxiv` | `biorxiv`, `medrxiv`, or `arxiv`. |
| `year` / `week` | current ISO | UTC. Override for backfills. |
| `categories` | "" | Comma-separated allowlist. See feed action's `docs/categories.md`. |
| `max_papers` | 0 | 0 = no cap. |
| `model` | `openai/gpt-4o-mini` | Any GitHub Models–supported id. |
| `enrich` | `true` | Toggles the abstract fetch + extraction pass. |
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
- `ARXIV_REQUEST_DELAY_SECS` (default `3.0`) — polite delay before each arxiv
  abstract fetch.
- `MODELS_URL` — override the chat-completions endpoint (default GitHub
  Models). Accepts any OpenAI-compatible URL.
- `OFFLINE` / `STUB_MODE` — stub the LLM in tests (`yes` / `no` / `hash` /
  `flaky`).

Secrets:

- None. The auto-provided `GITHUB_TOKEN` is used for both the `gh api` feed
  fetch and the GitHub Models REST POST. GitHub now scopes `models: read`
  through the standard token, so consumers only need to declare
  `permissions: models: read` on their calling workflow — no separate PAT.

## Determinism

- `temperature 0` on both passes for reproducibility week-over-week.
- Relevance prompt is constrained to a single token (`YES`/`NO`).
- Extraction prompt asks for a fixed JSON schema; non-JSON responses are
  preserved verbatim under `_raw` for triage rather than dropped.

## Servers and schema adapters

| Server | Producer CSV columns | Abstract source |
| --- | --- | --- |
| `biorxiv`, `medrxiv` | `Date,ISOWeek,DOI,Version,Category,Title,Authors` | `https://api.biorxiv.org/details/{server}/{doi}` (JSON) |
| `arxiv` | `Published,Weekday(Monday==0),Updated,ID,Version,Title` | `https://export.arxiv.org/api/query?id_list={id}` (Atom XML) |

The arxiv producer CSV is missing `Category` and `Authors`, so `--categories`
is a no-op for `--server=arxiv` (eval prints a WARN and resets the filter).
A pydantic `ArxivCsvRow` model validates the raw row and `to_paper()` maps
it onto the normalized `Paper` shape: arxiv id lands in `doi`, `iso_week` is
derived from `Published`, and the single-quoted title is unquoted.

### Why `defusedxml` for the arxiv Atom response

The arxiv abstract endpoint returns Atom XML. Stdlib
`xml.etree.ElementTree.fromstring` is vulnerable to known XML attacks
(billion laughs, quadratic blowup; Python 3.7.1+ mitigates XXE only). The
Python docs themselves recommend `defusedxml` as the canonical defense:
<https://docs.python.org/3/library/xml.html#the-defusedxml-package>.
Costs: one small pure-Python dep, no transitive deps, drop-in
(`defusedxml.ElementTree.fromstring` matches stdlib's signature; same
`ParseError`). Alternatives considered and rejected: `# nosec` (suppresses
without fixing), custom hardening (reinvents the wheel), regex parsing
(brittle).

### Outbound HTTP chokepoint

All non-Models GETs go through `_urlopen_bytes(url, timeout=30)`, which
refuses non-`https://` schemes. This keeps a single Bandit B310 suppression
site instead of sprinkling `# nosec` across every fetcher.

### FIXME — XML dependency removable once producer normalizes

The arxiv XML parse exists only because the producer CSV omits the abstract
(and `Category`/`Authors`). If `qte77/gha-rxiv-feed-action` evolves to emit
a unified normalized CSV that includes the abstract (and category)
pre-fetched on the producer side, the eval action can drop both the Atom
parse and the `defusedxml` dependency. Tracked alongside the producer
schema-unification discussion.

## Why a separate eval repo (vs. living in the feed repo)

- **Separation of concerns.** The feed action's job is to emit the CSV; eval
  policy (topic, categories, model) is per-consumer and changes more often
  than the producer.
- **Centralized prompt evolution.** Tweaks to the relevance/extraction prompt
  propagate to every consumer by tag.
- **Versioning.** Consumers pin the workflow's `uses:` ref to a tag, so the
  prompt contract is stable across runs until they explicitly bump. The
  workflow auto-derives its checkout repo + sha from `github.workflow_ref`
  / `github.workflow_sha`, so the script version always matches the
  workflow version the caller pinned.

## Open prototype questions

- **GitHub Models quotas.** Inference is rate / token-limited per GitHub plan.
  0.2.0 adds steady-state throttling (`RXIV_EVAL_LLM_CALL_INTERVAL_SECS`) and
  exponential backoff on 429/5xx, plus a >50%-failure-rate hard exit so
  rate-limited runs are visible. A cheap pre-filter to shrink the candidate
  set before LLM calls is still open (tracked in #7).
- **Custom extraction schemas per consumer.** Today the schema is hardcoded in
  `DEFAULT_EXTRACTION_PROMPT`. If schemas diverge a lot, externalize the prompt
  into a per-consumer file the workflow path-inputs.
- **Caching.** Same DOI evaluated across two consumers pays twice. A keyed
  cache (DOI → relevance verdict + extract) would be cheap to add later if it
  matters.

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

Outputs land in `/tmp/rxiv-eval/`. The script POSTs to the GitHub Models REST
endpoint directly (`https://models.github.ai/inference/chat/completions`); no
`gh` extension is required.
