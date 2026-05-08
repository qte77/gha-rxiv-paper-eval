# LLM provider research

Captured 2026-05-08. Question: should we stay on **GitHub Models** for the
relevance + extraction calls, or move to an alternative free / freemium
provider? Answer: **stay on GitHub Models**, with **Google AI Studio (Gemini
Flash-Lite)** as a designated backup if quotas bite.

## Sources (first-party)

- GitHub Models catalog API: <https://models.github.ai/catalog/models>
- GitHub Models inference endpoint: <https://models.github.ai/inference/chat/completions>
- GitHub Models docs: <https://docs.github.com/en/github-models>
- Google AI Studio: <https://aistudio.google.com>
- Groq: <https://groq.com>
- Cerebras: <https://www.cerebras.net>
- OpenRouter: <https://openrouter.ai>
- HuggingFace Inference Providers: <https://huggingface.co/docs/inference-providers>
- NVIDIA NIM: <https://build.nvidia.com/explore/discover>
- Mistral La Plateforme: <https://console.mistral.ai>
- Cohere: <https://cohere.com>
- Cloudflare Workers AI: <https://developers.cloudflare.com/workers-ai>

Cross-reference (third-party catalog, kept up to date by scraping the above):
[cheahjs/free-llm-api-resources](https://github.com/cheahjs/free-llm-api-resources).

## GitHub Models — highest context window

Pulled live from `https://models.github.ai/catalog/models`. Sorted by
`limits.max_input_tokens` desc:

| Model id | Input ctx | Output | Tier | Note |
| --- | ---: | ---: | --- | --- |
| `meta/llama-4-scout-17b-16e-instruct` | 10,000,000 | 4,096 | high | 10M context, but only 4K output |
| `openai/gpt-4.1` | 1,048,576 | 32,768 | high | 1M ctx + 32K output |
| `openai/gpt-4.1-mini` | 1,048,576 | 32,768 | **low** | same ctx as gpt-4.1 on the cheap tier |
| `openai/gpt-4.1-nano` | 1,048,576 | 32,768 | **low** | same again, smaller model |
| `meta/llama-4-maverick-17b-128e-instruct-fp8` | 1,000,000 | 4,096 | high | |
| `ai21-labs/ai21-jamba-1.5-large` | 262,144 | 4,096 | high | |
| `mistral-ai/codestral-2501` | 256,000 | 4,096 | low | |
| `openai/gpt-5*`, `openai/o1`/`o3`/`o4-mini` | 200,000 | 100,000 | custom | huge output, custom quota |

Tier counts: **low: 16** · **high: 10** · **custom: 15** · **embeddings: 2**.
"low" has the most generous request quota; "custom" tier (GPT-5, o-series)
has model-specific limits.

### What this means for us

Our prompts:
- Relevance: title + category, ~50 tokens in, 4 tokens out (`YES`/`NO`).
- Extraction: a single rxiv abstract, ~300–800 tokens in, ≤512 tokens JSON out.

We are **nowhere near** the context ceiling. Implications:

- `openai/gpt-4o-mini` (low tier, current default) is fine and we keep the
  generous `low`-tier rate.
- If we ever **batch** abstracts (e.g. score 100 titles in one call to amortize
  overhead), `openai/gpt-4.1-nano` gives 1M context **on the low tier** — same
  quota, much more headroom than `gpt-4o-mini`'s 128K.
- For *much* longer corpora (e.g. cluster a full week's 158 abstracts in one
  shot), only `meta/llama-4-scout` reaches 10M, but its 4K output cap and
  `high` tier rate-limit make that an edge tool, not a default.

## Alternative free providers — comparison

Filtered to providers that fit our shape (programmatic API, OpenAI-style chat,
no phone verification preferred, daily cap usable for a weekly cron).

| Provider | Best fit | Daily cap | Auth surface | Trade-off |
| --- | --- | --- | --- | --- |
| **GitHub Models** *(current)* | `gpt-4.1-nano`, `gpt-4o-mini` | per-GH-plan; `low` tier generous | `GH_TOKEN` (already in workflow) | Catalog narrower than aggregators |
| **Google AI Studio** | Gemini 3.1 Flash-Lite | **500 req/day**, 250K tok/min | new API key, separate secret | Data trained on outside UK/CH/EEA/EU (privacy concern); 1M+ ctx; very fast |
| **OpenRouter** | `:free` model variants (Llama, Qwen, GPT-OSS, etc.) | 50 req/day free, 1000/day with $10 lifetime topup | new API key | Aggregator — vendor-agnostic; small free quota |
| **Groq** | Llama 3.x | rate-limited (RPM) | new API key | Tens-of-tokens/sec; same OpenAI shape |
| **Cerebras** | Llama 3.x | rate-limited | new API key | Even faster than Groq |
| **HuggingFace Inference Providers** | many | rate-limited | HF token | Wide model catalog |
| **NVIDIA NIM** | various open models | 40 req/min | new API key + **phone verification** | Phone verification is a non-starter for org-wide CI |
| **Mistral La Plateforme** | Mistral open + proprietary | 1 req/sec; 500K tok/min | API key + phone | Free tier requires **opt-in to data training** |
| **Cohere** | Command-r | rate-limited | API key | OpenAI-compatible |
| **Cloudflare Workers AI** | various | rate-limited | account token | Tied to CF account |

Excluded as low-fit: trial-credit providers (Fireworks, Baseten, Nebius, etc.)
— time-limited, not appropriate for ongoing CI.

## Recommendation

**Stay on GitHub Models.** Reasons:

1. **Auth already wired.** The workflow uses `GH_TOKEN` for the feed-CSV
   `gh api` call; the same token serves Models inference (with `models: read`).
   No second secret, no second rate-limit budget to reason about.
2. **OpenAI-compatible REST.** The current script's REST POST is portable —
   moving to another OpenAI-compatible vendor is a base-URL + auth header
   change, not a rewrite. So the cost of *not* switching now is low.
3. **Headroom on the low tier.** If the catalog ever feels tight, swap
   `gpt-4o-mini` for `gpt-4.1-nano` — same low-tier quota, 1M context.
4. **Determinism.** Single vendor, single tokenizer behavior, single
   error-mode profile keeps weekly diffs reproducible.

**Designated backup: Google AI Studio (Gemini 3.1 Flash-Lite).** 500 req/day
is plenty for the weekly cron (we'd burn ≤ 158 requests). Switch trigger:
GitHub Models returns sustained 429s on the `low` tier. The privacy caveat
(data trained on outside EU) matters less for our case since input is
publicly-posted preprint titles + abstracts.

**Future "if we ever need it":**

- **OpenRouter** if we want to A/B different model families without changing
  auth surfaces every time.
- **Groq / Cerebras** if a future workload demands sub-second latency
  (current weekly batch doesn't).
- **HuggingFace** if we want to evaluate domain-specific fine-tunes (e.g.
  BioGPT-style biomedical models not on GitHub Models).

## What's *not* on the table

- **Anthropic Claude / OpenAI direct.** Not free. If/when we want them,
  Lambda-Biolab pays for the API key and we add it as an alternate provider —
  but that's a different conversation than "free providers".
- **Self-hosted.** Not justified at this scale (≤ 200 calls/week).
