# Release tooling for claude-subagents.
# Version lives in .claude-plugin/plugin.json and plugin.yaml and follows semver.
#
#   make bump-patch   # 0.1.0 -> 0.1.1
#   make bump-minor   # 0.1.1 -> 0.2.0
#   make bump-major   # 0.2.0 -> 1.0.0
#   make release      # commit the version bump and push to origin
#
# Typical flow:  make bump-minor && make release

SHELL := /usr/bin/env bash
PLUGIN_JSON ?= .claude-plugin/plugin.json
HERMES_PLUGIN_YAML ?= plugin.yaml
REVIEWERS_JSON := .claude-plugin/reviewers.json
CODERS_JSON := .claude-plugin/coders.json
PYTHON ?= python3

.PHONY: bump-patch bump-minor bump-major release version models check-version-sync

# Current version (e.g. "0.1.0").
version:
	@jq -r '.version' $(PLUGIN_JSON)

check-version-sync:
	@json_ver=$$(jq -r '.version' $(PLUGIN_JSON)); \
	yaml_ver=$$(awk '/^version:/ {print $$2; exit}' "$(HERMES_PLUGIN_YAML)"); \
	test -n "$$yaml_ver" || { echo "unable to read version from $(HERMES_PLUGIN_YAML)" >&2; exit 1; }; \
	test "$$json_ver" = "$$yaml_ver" || { echo "version drift: plugin.json=$$json_ver plugin.yaml=$$yaml_ver" >&2; exit 1; }

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
# zeroes the lower fields, and writes synchronized manifests.
.PHONY: _bump
_bump:
	@set -e; \
	success=0; \
	json_backup_ready=0; yaml_backup_ready=0; \
	json_tmp=""; yaml_tmp=""; json_bak=""; yaml_bak=""; \
	cleanup() { \
	  if [[ "$$success" -eq 0 ]]; then \
	    if [[ "$$json_backup_ready" -eq 1 ]]; then cp "$$json_bak" "$(PLUGIN_JSON)"; fi; \
	    if [[ "$$yaml_backup_ready" -eq 1 ]]; then cp "$$yaml_bak" "$(HERMES_PLUGIN_YAML)"; fi; \
	  fi; \
	  rm -f "$$json_tmp" "$$yaml_tmp" "$$json_bak" "$$yaml_bak"; \
	}; \
	trap cleanup EXIT; \
	$(MAKE) --no-print-directory check-version-sync \
	  PLUGIN_JSON="$(PLUGIN_JSON)" HERMES_PLUGIN_YAML="$(HERMES_PLUGIN_YAML)"; \
	old=$$(jq -r '.version' "$(PLUGIN_JSON)"); \
	IFS=. read -r MA MI PA <<< "$$old"; \
	case "$(PART)" in \
	  major) MA=$$((MA+1)); MI=0; PA=0 ;; \
	  minor) MI=$$((MI+1)); PA=0 ;; \
	  patch) PA=$$((PA+1)) ;; \
	  *) echo "unknown PART: $(PART)" >&2; exit 2 ;; \
	esac; \
	new="$$MA.$$MI.$$PA"; \
	json_tmp=$$(mktemp); \
	yaml_tmp=$$(mktemp); \
	json_bak=$$(mktemp); \
	yaml_bak=$$(mktemp); \
	cp "$(PLUGIN_JSON)" "$$json_bak"; \
	json_backup_ready=1; \
	if [[ "$${CSC_BUMP_FAIL_AFTER_JSON_BACKUP:-}" == "1" ]]; then exit 1; fi; \
	cp "$(HERMES_PLUGIN_YAML)" "$$yaml_bak"; \
	yaml_backup_ready=1; \
	jq --arg v "$$new" '.version = $$v' "$(PLUGIN_JSON)" > "$$json_tmp"; \
	awk -v ver="$$new" '/^version:/ {print "version: " ver; next} {print}' "$(HERMES_PLUGIN_YAML)" > "$$yaml_tmp"; \
	jq -e . "$$json_tmp" >/dev/null; \
	$(PYTHON) -c 'import sys, yaml; from pathlib import Path; data=yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")); assert isinstance(data, dict) and isinstance(data.get("version"), str)' "$$yaml_tmp"; \
	json_new=$$(jq -r '.version' "$$json_tmp"); \
	yaml_new=$$(awk '/^version:/ {print $$2; exit}' "$$yaml_tmp"); \
	test "$$json_new" = "$$new"; \
	test "$$yaml_new" = "$$new"; \
	mv "$$json_tmp" "$(PLUGIN_JSON)"; \
	json_tmp=""; \
	if [[ "$${CSC_BUMP_FAIL_AFTER_JSON:-}" == "1" ]]; then exit 1; fi; \
	mv "$$yaml_tmp" "$(HERMES_PLUGIN_YAML)"; \
	yaml_tmp=""; \
	$(MAKE) --no-print-directory check-version-sync \
	  PLUGIN_JSON="$(PLUGIN_JSON)" HERMES_PLUGIN_YAML="$(HERMES_PLUGIN_YAML)"; \
	echo "version: $$old -> $$new"; \
	if [[ "$${CSC_BUMP_FAIL_AFTER_OUTPUT:-}" == "1" ]]; then exit 1; fi; \
	success=1

# Commit the current version and push. Fails if there is nothing to commit.
release: check-version-sync
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
