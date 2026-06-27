# Build / test / run automation for gha-rxiv-paper-eval.
# Run `make help` to list recipes.

.SILENT:
.ONESHELL:
.PHONY: \
	sync setup \
	test lint complexity validate \
	smoke \
	help
.DEFAULT_GOAL := help


# -- paths --
SCRIPT := scripts/eval_papers.py

# -- smoke defaults (override on the command line) --
FEED_REPO ?= qte77/gha-rxiv-feed-action
SERVER    ?= arxiv
YEAR      ?=
WEEK      ?=
MAX       ?= 5
MAX_LLM_CALLS ?= 0
OUT       ?= /tmp/rxiv-eval

# -- quiet mode (default: quiet; VERBOSE=1 for full output) --
VERBOSE ?=
ifndef VERBOSE
  RUFF_QUIET   := --quiet
  PYTEST_QUIET := -q
endif


# MARK: SETUP

sync:  ## Install project + dev deps via uv
	uv sync

setup:  ## Install runtime deps only (matches CI's `uv sync --no-dev`)
	uv sync --no-dev


# MARK: QUALITY

test:  ## Run pytest
	echo "--- test"
	uv run pytest $(PYTEST_QUIET) tests/

lint:  ## Lint scripts and tests with ruff
	echo "--- lint"
	uv run ruff check $(RUFF_QUIET) scripts/ tests/

complexity:  ## Cognitive-complexity gate (max=10)
	echo "--- complexity"
	uv run complexipy scripts/

validate:  ## All quality gates: lint + complexity + test
	set -e
	$(MAKE) -s lint
	$(MAKE) -s complexity
	$(MAKE) -s test
	echo "=== validate: all passed ==="


# MARK: APP

smoke:  ## Local smoke test of the eval script against real APIs. Usage: make smoke [SERVER=arxiv] [YEAR=2024] [WEEK=24] [MAX=5] [MAX_LLM_CALLS=10]
	echo "--- smoke: $(SERVER) $(YEAR)-w$(WEEK) -> $(OUT)"
	GH_TOKEN=$$(gh auth token) uv run python $(SCRIPT) \
		--feed-repo $(FEED_REPO) \
		--server $(SERVER) \
		$(if $(YEAR),--year $(YEAR)) \
		$(if $(WEEK),--week $(WEEK)) \
		--max-papers $(MAX) \
		$(if $(filter-out 0,$(MAX_LLM_CALLS)),--max-llm-calls $(MAX_LLM_CALLS)) \
		--enrich \
		--output-dir $(OUT)


# MARK: HELP

help:  ## Show available recipes grouped by section
	@echo "Usage: make [recipe] [VAR=value ...]"
	@awk '/^# MARK:/ { \
		section = substr($$0, index($$0, ":")+2); \
		printf "\n\033[1m%s\033[0m\n", section \
	} \
	/^[a-zA-Z0-9_-]+:.*?##/ { \
		helpMessage = match($$0, /## (.*)/); \
		if (helpMessage) { \
			recipe = $$1; sub(/:/, "", recipe); \
			printf "  \033[36m%-12s\033[0m %s\n", recipe, substr($$0, RSTART + 3, RLENGTH) \
		} \
	}' $(MAKEFILE_LIST)
