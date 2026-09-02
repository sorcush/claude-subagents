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

## [2.3.0] - 2026-09-02

### Added
- add run-scoped worktree names
- add opt-in Cursor execution guards
- isolate delegated verification home
- add Hermes Cursor dispatch foundation
- add Hermes Cursor workflows

### Changed
- design Hermes Cursor subagents
- clarify run-scoped worktree reuse
- require safe run-scoped removal
- record Hermes Cursor smoke results
- compare Hermes Cursor and Superpowers workflows
- explain trusted Hermes plugin installation
- Revert Hermes Cursor subagents work

### Fixed
- reject run-scoped feature slug collisions
- validate run-scoped removal origin
- handle Cursor stream boundaries safely
- reject symlinked verification home
- reject symlink traversal in verification home
- harden Hermes Cursor smoke lifecycle
- make Hermes plugin installable and discoverable
- report real changed/result on harness-invocation failure
- preserve session id when a coder run times out
- gate coder-delegator commits on real worktree isolation, not branch name

## [2.2.0] - 2026-08-23

### Changed
- **BREAKING:** rename plugin to claude-subagents

## [2.1.0] - 2026-08-23

### Added
- add cx-delegate.sh — Codex CLI delegate for the review script trio
- add codex-reviewer-delegator agent and /codex-review command
- reviewer and coder pool files with a validating library
- portable bounded execution helper with process-group cleanup
- harness layer for cursor, codex and claude, plus probe.sh
- generic review delegate over the reviewer pool
- generic code delegate that works inside a worktree
- worktree lifecycle with copy-on-write dependency cloning
- model-agnostic reviewer and coder delegator subagents
- model-agnostic /review and /implement-plans commands
- **BREAKING:** remove sync-models machinery and the tool-specific commands

### Changed
- log cursor-coder delegation effectiveness for the changelog/model-config run
- add design spec for the Codex/GPT-5.6 Sol reviewer
- fix codex-reviewer spec per independent Cursor/Grok review
- log codex-reviewer spec review effectiveness
- add implementation plan for the Codex/GPT-5.6 Sol reviewer
- guard the Codex reviewer's dynamic --model probes against hardcoded regressions
- document the Codex reviewer in README and e2e-smoke.md
- normalize Codex reviewer descriptions/markers via sync-models.sh
- design spec for reviewer and coder pools
- confirm real model ids in the pools spec
- revise pools spec after Codex review
- log the Codex review of the pools spec
- revise pools spec after second Codex review
- log the second Codex review round
- implementation plan for reviewer and coder pools
- revise plan and spec after the Codex plan review
- log the third Codex review round
- pre-flight scan fixes to the pools plan
- harden review-delegate failure and spec-file validation checks
- strengthen code-delegate verify output and session id checks
- delegation effectiveness log for the pools implementation
- make all test files executable and ignore the SDD scratch tree

### Fixed
- make sync-models.sh's regen_markers a three-way dispatch
- sync-models.sh was silently stripping +x off delegate scripts
- optimize pool.sh performance with single-pass validation and add safety tests
- add zombie check to timeout guard and document untested cases
- complete task 3 mocks and harden progress-event probe test
- harden harness contract for codex, cursor, and classifier
- purge nested secrets in copied dependency folders
- tighten pool.sh guard and restore isolated worktree wording
- harden worktree isolation and harness probe/coder behavior

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
