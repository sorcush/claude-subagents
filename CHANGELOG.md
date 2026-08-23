# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## 2.0.0

### Breaking
- The three tool-specific commands are removed and replaced by two:
  - `/cursor-review` and `/codex-review` → `/review`
  - `/cursor-implement-plans` → `/implement-plans`
  There are no aliases. Both new commands ask which worker to use.
- `.claude-plugin/models.json` is removed. Models now live in
  `.claude-plugin/reviewers.json` and `.claude-plugin/coders.json`.
- `scripts/sync-models.sh` and its two tests are removed. Nothing needs
  regenerating: the orchestrator reads the pool at run time.

### Added
- Configurable reviewer and coder pools; adding a model is one JSON entry.
- A harness layer (`scripts/harness/`), so adding a tool is one file with three
  functions. Claude joins Cursor and Codex as a supported tool.
- The coder now works in an isolated git worktree. Dependency folders are
  copy-on-write clones, so it cannot write into the main checkout.
- Bounded timeouts on every external call, terminating the whole process group.

### Fixed
- A resumed Codex review silently lost its read-only sandbox, because
  `codex exec resume` accepts neither `-C` nor `-s`. It now passes
  `-c sandbox_mode="read-only"`.
- The verify command ran in the caller's directory rather than the coder's.

## [1.2.0] - 2026-07-18

### Added
- add models.json as the single source of truth for coder/reviewer models
- read coder model id from models.json in cc-delegate.sh
- read reviewer model id from models.json in cr-delegate.sh
- add gen-changelog.sh to generate a Keep a Changelog section from commits
- add update-changelog.sh for idempotent replace-in-place changelog writes
- wire changelog generation and models drift-check into make release
- add sync-models.sh (config validation + templated-field regeneration)
- add marker-span regeneration, --check, and write atomicity to sync-models.sh
- roll out models.json markers/dynamic reads, fix stale GPT-5.5 references

### Changed
- add changelog + per-agent model config design spec
- revise changelog/model-config spec per Grok 4.5 review
- fix changelog idempotency and remaining spec contradictions
- fix e2e-probe marker defect and tighten remaining spec ambiguities
- fix changelog insert-point bug and complete marker inventory
- log cursor-reviewer effectiveness for the 4-round spec review
- add implementation plan for changelog tracking + model config
- fix plan's scratch-clone verification script (dirty-tree trap)
- guard the dynamic --model probes against hardcoded regressions
- list the new test files in README's Tests section

### Fixed
- correct update-changelog.sh section-replacement ordering bug
- use // empty in jq -er probes so a missing model id fails loudly instead of passing the literal string null
- unify atomic writes across sync-models.sh, enforce ASCII labels, guard release pipe
