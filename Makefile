# Release tooling for claude-subagents.
# Version lives in .claude-plugin/plugin.json and follows semver (MAJOR.MINOR.PATCH).
#
#   make bump-patch   # 0.1.0 -> 0.1.1
#   make bump-minor   # 0.1.1 -> 0.2.0
#   make bump-major   # 0.2.0 -> 1.0.0
#   make release      # commit the version bump and push to origin
#
# Typical flow:  make bump-minor && make release

SHELL := /usr/bin/env bash
PLUGIN_JSON := .claude-plugin/plugin.json
REVIEWERS_JSON := .claude-plugin/reviewers.json
CODERS_JSON := .claude-plugin/coders.json

.PHONY: bump-patch bump-minor bump-major release version models

# Current version (e.g. "0.1.0").
version:
	@jq -r '.version' $(PLUGIN_JSON)

# Pretty-print the configured reviewer and coder pools.
models:
	@echo "reviewers:"; jq . $(REVIEWERS_JSON)
	@echo "coders:";    jq . $(CODERS_JSON)

bump-patch:
	@$(MAKE) --no-print-directory _bump PART=patch

bump-minor:
	@$(MAKE) --no-print-directory _bump PART=minor

bump-major:
	@$(MAKE) --no-print-directory _bump PART=major

# Internal: PART={major|minor|patch}. Reads version, increments the right field,
# zeroes the lower fields, and writes it back to plugin.json.
.PHONY: _bump
_bump:
	@old=$$(jq -r '.version' $(PLUGIN_JSON)); \
	IFS=. read -r MA MI PA <<< "$$old"; \
	case "$(PART)" in \
	  major) MA=$$((MA+1)); MI=0; PA=0 ;; \
	  minor) MI=$$((MI+1)); PA=0 ;; \
	  patch) PA=$$((PA+1)) ;; \
	  *) echo "unknown PART: $(PART)" >&2; exit 2 ;; \
	esac; \
	new="$$MA.$$MI.$$PA"; \
	tmp=$$(mktemp); \
	jq --arg v "$$new" '.version = $$v' $(PLUGIN_JSON) > "$$tmp" && mv "$$tmp" $(PLUGIN_JSON); \
	echo "version: $$old -> $$new"

# Commit the current version and push. Fails if there is nothing to commit.
release:
	@ver=$$(jq -r '.version' $(PLUGIN_JSON)); \
	if git diff --quiet && git diff --cached --quiet; then \
	  echo "nothing to release: working tree clean (did you run a bump target?)" >&2; exit 1; \
	fi; \
	for t in tests/test-*.sh; do \
	  bash "$$t" >/dev/null || { echo "tests failing: $$t" >&2; exit 1; }; \
	done; \
	set -o pipefail; \
	scripts/gen-changelog.sh --version "$$ver" | scripts/update-changelog.sh --version "$$ver"; \
	git add -A; \
	git commit -m "release: v$$ver"; \
	git push; \
	echo "released v$$ver"
