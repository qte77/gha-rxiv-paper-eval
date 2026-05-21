# Roadmap

Buckets ordered by priority. Items reference the canonical tracker issue.

## Now (in flight or queued)

- **IRL arxiv demo** — trigger `eval-papers-dispatch.yaml` against a
  current 2026 week and verify end-to-end output. Gated on PRs #26
  (timeout catch) and #27 (Makefile) landing.
- **Scheduled qte77/qte77 watchlist** — wire the consumer workflow in
  the profile repo (`qte77/qte77`#96).

## Next (small, scoped)

- **#6** Critical: 429 rate-limit failures masquerade as "0 hits" — make
  weekly runs detect rate-limit drops and surface them instead of
  silently returning empty.
- **#5** Default relevance system prompt is too conservative — reduce
  false negatives on methodology papers.
- **#7** Reduce LLM request count via a cheap pre-filter (keyword /
  embedding) before the relevance classifier.
- **#14** Self-test: weekly smoke results — surface the dispatch wrapper's
  output as a recurring health check.

## Later (architectural)

- **#12** Multi-provider matrix in one run (output_suffix + matrix
  strategy). Lets a single run compare GitHub Models vs Anthropic vs
  others.
- **#11** Anthropic Batch API support (50% discount, async). Combine
  with #12 for cost-aware sweeps.
- **#10** Two-stage funnel: cheap model for relevance (Haiku),
  expensive model for extraction only on YES hits.
- **Configurable model endpoint** — generalize `GITHUB_MODELS_URL` to an
  env/Settings field so consumers can point at OpenAI-compatible
  providers. Touched on in PR #16's session; not yet a tracker issue.

## Upstream / cross-repo

- **`qte77/gha-rxiv-feed-action`#107** — align the arxiv producer CSV
  schema with the unified `Date,ISOWeek,DOI,Version,Category,Title,Authors`
  shape (and optionally pre-fetch the abstract). When it lands, the
  eval action can drop the `ArxivCsvRow` adapter, the Atom XML parser,
  and the `defusedxml` dependency. Tracked by the `# FIXME` above the
  defusedxml import in `scripts/eval_papers.py`.

## Done (recent, see [CHANGELOG.md](CHANGELOG.md))

- `--server arxiv` with pydantic schema adapter (#16).
- Default topic targeting qte77 themes (#21).
- Workflow auto-derives checkout repo from `github.workflow_ref` (#22).
- GitHub Models auth via the auto-provided `GITHUB_TOKEN` (#24).
- Full strict ruff ruleset (#17 + #20).
- Setup-uv bumped to Node 24 (#25).
