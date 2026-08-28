# Hermes Cursor Subagents Design

**Date:** 2026-08-28  
**Status:** Approved design  
**Repository:** `sorcush/claude-subagents`

## Summary

Extend this repository into a dual-host repository for Claude Code and Hermes Agent. Claude Code retains its existing commands and delegator agents. Hermes receives two native plugin skills, `cursor-coder` and `cursor-reviewer`, which call the existing deterministic worker harness directly.

The design deliberately duplicates host-level workflow instructions while keeping execution mechanics shared. The repository will document and test the duplicated adapter boundary so future maintainers know when a change must be applied to both hosts.

## Goals

1. Add a `cursor-coder` Hermes role for written-plan implementation and ad-hoc bounded coding tasks.
2. Add a `cursor-reviewer` Hermes role for design-specification and implementation-plan review.
3. Keep Cursor model selection profile-scoped and independently configurable for each role.
4. Keep every edit-capable Cursor run isolated in a git worktree.
5. Integrate work only after independent controller review and verification.
6. Preserve Cursor session IDs for resumed corrections and re-reviews.
7. Reuse the existing pool, harness, timeout, review-rubric, and worktree implementation.
8. Preserve the behavior of the existing Claude Code plugin.
9. Make adapter duplication explicit to future coding agents and maintainers.

## Non-goals

The initial implementation will not:

- configure Cursor as a Hermes inference provider;
- use Hermes `delegate_task` to wrap Cursor;
- register new model-facing Hermes tools;
- add Codex or Claude workers to the Hermes roles;
- make `cursor-reviewer` a general code-review or repository-analysis role;
- run multiple edit-capable workers concurrently;
- broadly refactor the existing shared scripts; planned shared-script changes are opt-in run-scoped worktree naming, Cursor sandbox and stream limits, and a verification-specific home whose unset behavior preserves the Claude path;
- move or replace the existing Claude pool files;
- modify any Hermes profile other than `super-dev-codex` during installation and verification;
- push, publish, merge remotely, rewrite history, or release without a separate explicit request.

Invoking `cursor-coder` explicitly authorizes local commits inside the isolated worktree and their fast-forward integration after controller verification and review. It does not authorize any remote side effect.

## Existing Architecture

The current Claude Code flow has four layers:

```text
Claude Code controller
  -> command workflow
    -> restricted delegator subagent
      -> deterministic shell harness
        -> Cursor, Codex, or Claude CLI
```

The delegator subagent translates Claude-specific orchestration into a shell call. The shell layer owns the durable behavior:

- worker-pool resolution;
- real readiness probes;
- external CLI invocation;
- session continuation;
- verification and bounded correction attempts;
- timeout and process-group handling;
- worktree isolation;
- structured JSON results.

The existing harness passed 285 assertions across 10 test files under Bash 5 during design investigation. Real probes for the configured `cursor-composer` coder and `cursor-sol` reviewer returned `READY` after Cursor authentication was repaired.

## Target Architecture

```text
Hermes controller
  -> registered Hermes workflow skill
    -> Hermes configuration wrapper
      -> existing deterministic shell harness
        -> Cursor Agent CLI
          -> isolated repository worktree for edit tasks
```

Hermes calls the deterministic harness directly. It does not insert a native Hermes child between the controller and Cursor because that child would add another model call without changing Cursor into a Hermes provider.

### Components

| Component | Responsibility |
|---|---|
| Hermes skill | Controller workflow, preflight, task preparation, review gates, and result handling |
| Hermes wrapper | Resolve profile configuration, select Bash 5, construct a temporary pool, and call existing scripts |
| Existing delegate scripts | Worker invocation, retry, verification, and structured result production |
| Existing Cursor harness | Cursor arguments, read-only mode, edit mode, and session resume |
| Existing worktree script | Worktree creation, dependency copying, cleanup, and loss prevention |
| Hermes controller | Inspect diffs, run independent verification, commit verified worker changes inside the worktree, review those commits, and authorize integration |
| Cursor worker | Perform only the assigned implementation or document review |

## Native Hermes Plugin Packaging

The repository root becomes a valid native Hermes plugin while retaining its existing Claude Code plugin structure.

Planned additions:

```text
plugin.yaml
__init__.py

hermes/
├── skills/
│   ├── cursor-coder/
│   │   └── SKILL.md
│   └── cursor-reviewer/
│       └── SKILL.md
└── scripts/
    └── dispatch.py

AGENTS.md
CLAUDE.md
docs/adapter-parity.md

tests/
├── test-hermes-plugin.sh
├── test-adapter-parity.sh
├── adapter-contract.json
└── test-version-sync.sh
```

The native manifest uses `name: claude-subagents`, `manifest_version: 2`,
`api_version: 1`, the same semantic version as `.claude-plugin/plugin.json`,
and a `superpowers` plugin dependency constrained to `>=6.3.0,<7.0.0`. Version 2.2.0 is the
design baseline; the release that introduces Hermes support receives the next
appropriate semantic version. The supported Hermes baseline is the verified
v0.20.6 runtime. Older Hermes releases receive no compatibility claim until
tested.

The root `__init__.py` exports only `register(ctx)`. It parses each Hermes
skill's frontmatter for metadata and calls `ctx.register_skill(name, path,
description, frontmatter)`. It registers no tools, hooks, commands, providers,
or background services. Missing or incompatible `superpowers`, malformed
frontmatter, a missing skill file, or either failed registration makes plugin
registration fail closed with a diagnostic naming the dependency or skill.

`__init__.py` registers the skills from their Hermes-specific paths. The expected qualified skill names are:

```text
claude-subagents:cursor-coder
claude-subagents:cursor-reviewer
```

The plugin registers no custom model-facing tools. Skills use the existing `terminal`, file, todo, and Superpowers workflows.

Claude Code only auto-discovers its defined top-level plugin components, including `.claude-plugin/`, `commands/`, `agents/`, `skills/`, `hooks/`, `.mcp.json`, `.lsp.json`, `monitors/`, `bin/`, and a root `SKILL.md`. Hermes skills live under `hermes/skills/`, so Claude Code does not discover them as Claude plugin skills.

## Profile-Scoped Model Configuration

The native Hermes manifest declares two required string settings through `config_schema`:

```yaml
config_schema:
  coder_model:
    type: str
    description: Cursor model used by cursor-coder
    required: true
  reviewer_model:
    type: str
    description: Cursor model used by cursor-reviewer
    required: true
```

Hermes stores these settings in the active profile:

```yaml
plugins:
  entries:
    claude-subagents:
      settings:
        coder_model: composer-2.5
        reviewer_model: gpt-5.6-sol-high
```

The setup commands for this profile are:

```bash
hermes -p super-dev-codex config set \
  plugins.entries.claude-subagents.settings.coder_model \
  composer-2.5

hermes -p super-dev-codex config set \
  plugins.entries.claude-subagents.settings.reviewer_model \
  gpt-5.6-sol-high
```

The plugin declares no fallback model. Missing, empty, or rejected models stop dispatch.

A normal Hermes update preserves the profile's `config.yaml`. A plugin update also leaves these profile settings outside the plugin checkout. Configuration-key names are therefore public compatibility interfaces. A future rename requires an explicit migration; Hermes cannot infer renamed plugin settings.

## Configuration Wrapper

The Hermes-only wrapper performs the following steps:

1. Accept a role, operation, workspace, task inputs, verification command, and optional session ID.
2. Resolve the active profile through inherited `HERMES_HOME`.
3. Read the role-specific model using `hermes config get plugins.entries.claude-subagents.settings.<role>_model`.
4. Reject missing or blank values.
5. Construct a temporary one-entry JSON pool matching the existing pool schema.
6. Set `CSC_CODERS_JSON` or `CSC_REVIEWERS_JSON` to that temporary file.
7. Invoke the existing probe or delegate script using a verified Bash 5 executable.
8. Preserve the existing delegate script's stdout result contract.
9. Send progress and diagnostics to stderr.
10. Remove temporary files on success, failure, interruption, or timeout.

The wrapper does not parse or edit `config.yaml` itself. `hermes config get` is the authoritative profile-aware reader.

The Hermes adapter wrapper is a Python 3 standard-library program. Python owns
argument validation, profile configuration lookup, JSON validation, temporary
files, state persistence, file locking, subprocess limits, and Bash discovery.
It invokes the existing Bash scripts with the selected Bash 5 executable.

### Exact wrapper interface

```text
dispatch.py probe  --role coder|reviewer
                   --run-id <16-lowercase-hex>
dispatch.py worktree --action prepare --repo <controller-checkout>
                   --run-id <16-lowercase-hex>
dispatch.py worktree --action remove --repo <controller-checkout>
                   --run-id <16-lowercase-hex> --expected-generation <integer>
dispatch.py state  --action show
                   --run-id <16-lowercase-hex>
dispatch.py state  --action record-reviewing|record-integration|block|complete
                   --run-id <16-lowercase-hex>
                   [--commit <sha> only with record-integration]
                   --expected-generation <integer>
dispatch.py state  --action prune
dispatch.py code   --repo <controller-checkout> --cwd <worktree>
                   --run-id <16-lowercase-hex> --task-file <path>
                   --expected-generation <integer>
                   --verify-cmd <non-empty-command>
                   [--session <cursor-session-id>] [--max-retries <0..3>]
dispatch.py review --target spec|plan --doc-file <path>
                   --run-id <16-lowercase-hex>
                   [--expected-generation <integer>]
                   [--spec-file <path>] [--lenses <csv>]
                   [--session <cursor-session-id>]
```

The generated pool keys are exactly `cursor-coder` and `cursor-reviewer`.
Role and operation values outside the forms above are usage errors.
The Hermes controller generates one cryptographically random run ID at workflow
start and passes that exact value to prepare, probe, every initial or resumed
dispatch, integration diagnostics, and removal. The wrapper returns the same
value unchanged. A wrapper invocation never generates a replacement run ID.
Initial worktree preparation and an initial review create generation 1 and do
not accept `--expected-generation`. Every later state-mutating operation requires
the latest generation and returns the incremented value.

Exit codes are:

- `0`: successful probe, verified code result, or reviewed document;
- `1`: structured worker or verification failure;
- `2`: invalid arguments, missing configuration, unsupported runtime, or missing dependency;
- `128 + signal`: interruption after temporary-file cleanup.

Probe output uses the existing `probe.sh` fields plus `run_id`:

```json
{"status":"READY","run_id":"0123456789abcdef","role":"coder","key":"cursor-coder","reason":"","diagnostic":""}
```

Worktree prepare returns exactly
`{status,run_id,generation,worktree,work_branch,feature_branch,copied,skipped,neutralized_symlinks,purged_secrets,clone_supported,diagnostic}`.
Worktree remove returns exactly
`{status,run_id,work_branch,unmerged,generation,diagnostic}`. State action
`show` returns `{status,run_id,generation,record,diagnostic}`. State mutations
return `{status,run_id,state,generation,diagnostic}`. Global `prune` returns
`{status,removed,diagnostic}` and accepts no run ID. Code and review output uses the existing delegate schema plus top-level
`run_id` and `diagnostic` fields. `diagnostic` is empty on success and contains
the failure reason on wrapper-generated `BLOCKED` results. The wrapper validates the JSON before returning it. A code
result is successful only when `status == "DONE"`, `verified == true`, and
`session_id` is non-empty. A review result is successful only when
`status == "REVIEWED"`, `report` is non-empty, and `session_id` is non-empty.
Otherwise the wrapper returns `BLOCKED`, exit code 1, and a diagnostic naming
the failed invariant.

The wrapper uses `umask 077` for temporary files and never prints configuration
or environment contents. It caps the delegate's final JSON result at 2 MiB;
oversized final results fail as `BLOCKED` rather than being silently truncated.
The wrapper sets `CSC_STREAM_MAX_BYTES` to 64 MiB. The shared Cursor harness
enforces that optional limit while streaming into its internal temporary file,
terminates the Cursor process group on overflow, deletes the partial file, and
returns a structured failure. When `CSC_STREAM_MAX_BYTES` is unset, current
Claude behavior remains unchanged. Existing terminal spillover remains
responsible for display-side truncation of otherwise valid output.

Timeouts retain the existing harness defaults: 120 seconds for a real probe and
1,800 seconds for each Cursor call. The Hermes wrapper additionally imposes a
7,800-second total ceiling on one code dispatch, covering the initial call,
three correction calls, and verification; review has a 1,800-second total
ceiling. Timeout handling terminates the complete descendant process group and
returns a structured failure.

## Bash Runtime Selection

The current Hermes terminal environment resolves its first `bash` to Apple Bash 3.2. The existing pool library uses `mapfile`, and the repository documents Bash 5 as a requirement. Homebrew Bash 5.3.9 is installed at `/opt/homebrew/bin/bash` on the development machine.

The Python wrapper's Bash resolver must:

1. Check trusted absolute installation paths first: `/opt/homebrew/bin/bash`,
   `/usr/local/bin/bash`, `/usr/bin/bash`, and `/bin/bash`.
2. Enumerate remaining absolute `bash` candidates on `PATH`, rejecting empty or
   relative PATH entries and candidates located inside the target repository.
3. Select the first allowlisted candidate whose major version is at least 5.
4. Reject every candidate older than Bash 5.
5. Fail with an actionable installation message if no valid interpreter exists.
6. Execute existing scripts with the selected absolute interpreter path.

“Allowlisted” means path-based installation trust only. It does not assert
ownership or immutability of Homebrew-managed files.

The initial implementation does not change existing script shebangs or remove their Bash 5 requirement.

## Cursor Coder Role

`cursor-coder` supports two modes.

### Ad-hoc task mode

1. Require a git repository, a non-main feature branch, and a clean controller checkout.
2. Require a self-contained bounded task.
3. Require a non-empty explicit verification command. Derive it from repository evidence when possible and ask once when no defensible command can be derived. Stop without dispatch when none is available.
4. Prepare an isolated worktree using the existing worktree script.
5. Probe the configured Cursor coder model with a real model call.
6. Dispatch the task through the existing code delegate.
7. Require `status == "DONE"`, `verified == true`, and a non-empty Cursor session ID.
8. Inspect the changed-file scope and actual diff in the worktree.
9. Run independent controller verification in the worktree.
10. Commit the verified changes inside the worktree. The existing delegate script does not commit.
11. Perform controller review against the worktree commit. The implementation worker never approves its own work.
12. Resume the same Cursor session for bounded correction rounds and commit each verified correction inside the worktree.
13. Integrate only reviewed commits using fast-forward semantics.
14. Remove the worktree only after proving that no unintegrated commits would be lost.

### Written-plan mode

1. Perform the same branch, cleanliness, worktree, configuration, and probe preflight.
2. Read the complete plan and its referenced specification.
3. Follow the installed `subagent-driven-development` workflow.
4. Replace only its implementer boundary with the external `cursor-coder` dispatch.
5. Execute tasks serially in one dedicated worktree.
6. After each successful worker result, independently verify and commit the task changes inside the worktree.
7. Keep task review, code-quality review, final review, rulings, and verification under the Hermes controller.
8. Resume the same Cursor session for correction rounds on a task and commit each verified correction inside the worktree.
9. Fast-forward each reviewed task commit to the feature branch.
10. Preserve the existing cleanup refusal for unintegrated commits.
11. Finish through the normal branch-completion workflow.

The initial implementation does not parallelize writers. A bounded ad-hoc task
is one self-contained task with one verification command. The underlying code
delegate performs at most three verification-driven Cursor correction attempts.
The controller's review loop follows `subagent-driven-development` and remains
capped at five reviewed correction rounds, with its existing escalation policy.
The initial dispatch may make at most four Cursor calls: one implementation call
and three verification corrections. Each later controller review correction is
dispatched with `--max-retries 0`, so one task is capped at nine Cursor calls and
five hours of active subprocess time across the initial dispatch and all review
rounds. Controller reasoning and user wait time do not consume this ceiling.
Crossing either task-level limit produces `BLOCKED` and preserves the worktree.

### Concurrent-session isolation

Every Hermes coder workflow passes its controller-generated `run_id` to the
existing worktree script through an optional `CSC_RUN_ID`. The value must match
`^[a-f0-9]{16}$`; any other value is rejected. When set, the work branch is
`<feature-slug-prefix>-hermes-<run_id>-work` and the sibling directory is
`<repo>-<feature-slug-prefix>-hermes-<run_id>-work`. The feature slug replaces
every character outside `[A-Za-z0-9._-]` with `-`, collapses repeated dashes,
trims leading and trailing punctuation, substitutes `branch` when the result is
empty, and truncates to 80 characters before adding the suffix. A clean
registered worktree may be reused only when its stored original feature branch
and run ID match the current request. A path or branch collision from a
different original feature branch fails. Run-scoped removal performs the same
identity check before deleting the worktree or branch. When `CSC_RUN_ID` is unset, current Claude naming and reuse
behavior is unchanged. Tests must cover two concurrent run IDs and the legacy
unset path.

This optional extension is the only planned behavioral change to an existing
shared runtime script. It prevents two Hermes sessions, and a Hermes session
and a legacy Claude session, from sharing one worktree.

Before an edit-capable dispatch, the wrapper canonicalizes `--repo` and `--cwd`
and proves all of the following: both belong to the same Git common directory;
`--cwd` is a registered linked worktree; it differs from the controller
checkout; and its current branch is the exact run-scoped branch derived from
the feature branch and run ID. Validation occurs before Cursor starts.

The external Cursor process is instructed not to commit, switch branches, or
modify Git configuration, hooks, refs, remotes, or worktree registrations.
Authorization for local commits belongs to the Hermes controller role, not to
the external Cursor process. Before every dispatch, the wrapper records all
refs and object IDs, repository and worktree configuration, hook path/type/mode/
content hashes, registered worktrees, the controller branch and HEAD, and the
run worktree branch and HEAD. After dispatch it requires every value to remain
unchanged. The run worktree's index and working-tree files may change; refs may
not. Any unauthorized Git-control-plane change produces `BLOCKED`, preserves
the worktree and evidence, and prevents integration.

Hermes coder dispatch sets `CSC_CURSOR_SANDBOX=enabled`; the shared Cursor
harness translates that opt-in value to `--sandbox enabled`. The wrapper also records a path/type/mode/symlink/
content manifest for every tracked and non-ignored untracked file in the
controller checkout and compares it after dispatch. It runs `git fsck --full`
after dispatch to verify reachable object integrity. New unreachable objects are
permitted; changed refs, damaged reachable objects, or controller source-tree
changes block integration. These controls rely on Cursor's documented sandbox
plus postcondition checks; they do not claim protection from a malicious local
process with the user's full operating-system identity.

### Partial-plan recovery

Reviewed task commits that were already fast-forwarded remain on the feature
branch if a later task fails. Hermes does not revert them. The failed run's
worktree remains available for inspection, together with any unintegrated
commits. The final report must name the feature branch, work branch, worktree,
last integrated commit, and unintegrated commits, and provide explicit inspect,
resume, and safe-remove commands. The report also includes the stable run ID and
the last Cursor session ID. The wrapper persists those identifiers and worktree
paths in `$HERMES_HOME/claude-subagents/runs/<run_id>.json` with mode `0600`
inside a `0700` directory so a later controller session can recover the run
without reconstructing state.

The state record has this schema:

```json
{
  "schema_version": 1,
  "run_id": "0123456789abcdef",
  "role": "coder",
  "state": "reviewing",
  "generation": 3,
  "repository": "/absolute/controller/path",
  "cursor_session_id": "opaque-session-id",
  "coder": {
    "feature_branch": "feature/name",
    "worktree": "/absolute/worktree/path",
    "work_branch": "feature-name-hermes-0123456789abcdef-work",
    "pending_commit": null,
    "last_integrated_commit": "full-git-sha-or-empty",
    "unintegrated_commits": []
  },
  "reviewer": null,
  "failure": null,
  "updated_at": "RFC3339 timestamp"
}
```

Reviewer records use the same common fields with `coder: null` and:

```json
"reviewer": {
  "target": "spec",
  "document": "docs/spec.md",
  "specification": null,
  "lenses": ["backend"],
  "snapshot_outcome": "unchanged|changed|failed",
  "snapshot_path": null
}
```

All common and role keys are always present. Inapplicable role payloads and
optional scalar values are JSON `null`; they are never omitted or represented
as empty strings.

Every state read-modify-write uses Python's standard-library `fcntl.flock` on
`$HERMES_HOME/claude-subagents/runs/<run_id>.lock`, created with mode `0600`.
This avoids depending on a `flock` executable, which macOS does not ship. Every mutation
requires the generation last returned to the controller and increments it;
stale generations fail. The lock is process-scoped, so a crashed process
releases it automatically; pruning removes an unlocked stale lock file with its
eligible state record. The wrapper owns `prepared` and `dispatching` transitions. The Hermes skill
invokes `record-reviewing` before controller review, `record-integration`
immediately after each successful fast-forward, `block` when execution stops
with preserved state, and `complete` after safe worktree removal. Updates use a
mode-0600 temporary file, `fsync`, and atomic rename.
Allowed transitions begin `prepared -> dispatching -> reviewing`,
`reviewing -> dispatching` for a requested correction,
`reviewing -> integration_pending -> integrated` for accepted work,
`integrated -> dispatching` for the next plan task, and `integrated -> complete`
after final cleanup. Any non-complete state may transition to `blocked`.
Resumption moves `blocked -> dispatching` only after the wrapper validates the
stored repository, branch, worktree, run ID, and Cursor session ID.
Before fast-forward, the controller records the candidate commit in
`integration_pending`. Reconciliation is idempotent: if the feature branch
already contains that candidate, state advances to `integrated`; if it remains
an ancestor of the work branch, the controller may repeat the same
fast-forward; any divergence blocks. A crash after a controller commit but
before `record-reviewing` is reconciled by detecting exactly one new commit on
the unchanged run branch and moving to `reviewing`; any other branch movement
blocks. A crash while state says `dispatching` compares the recorded pre-dispatch
manifests with current Git and filesystem state and moves to `blocked` with the
observed delta. A crash after `record-reviewing` resumes review without another
implementation dispatch. If worktree removal completed before state
was marked complete, absence of both the registered worktree and work branch
reconciles to `complete`. Malformed or mismatched state fails closed and is preserved for diagnosis.
State updates synchronize both the temporary file and containing directory
before and after atomic rename.
Successful runs delete their record after completion. Failed records remain
until explicit cleanup; `state --action prune` removes only records older than
30 days whose worktrees no longer exist. Failure messages are redacted and
capped at 8 KiB; artifact paths must remain below the plugin state or temporary
review roots. Cursor session IDs are treated as sensitive opaque values. They
may appear in designated structured result and recovery outputs, but never in
ordinary progress logs or diagnostics.

## Cursor Reviewer Role

`cursor-reviewer` accepts exactly two targets:

- design specification;
- implementation plan.

The workflow:

1. Require a repository with a valid `HEAD`. Require a readable, regular,
   non-symlink document inside that repository. For plan review, apply the same
   rule to the optional specification. Reject unborn and non-Git repositories.
   Require the checkout to be clean except for the declared review document and
   optional specification.
2. For plan review, accept the associated specification path when present.
3. Determine the existing review rubric and applicable specification lenses.
4. Read the configured reviewer model.
5. Probe the model with a real Cursor call.
6. Invoke the existing review delegate in read-only mode.
7. Return the report verbatim with its Cursor session ID.
8. Treat an empty report or missing session ID as failure.
9. Verify that the disposable review snapshot did not change.
10. Resume the same Cursor session for re-review after document corrections.
11. Leave finding adjudication to the Hermes controller using the normal review-response workflow.

The reviewer does not perform general code-diff review or unrestricted repository analysis in the initial version.

Cursor documents `--mode ask` as read-only. By explicit design decision, the
shared Cursor harness retains its current `--force --trust --approve-mcps`
flags. The adapter therefore treats Cursor's ask-mode guarantee as the write
boundary; it does not claim operating-system sandbox isolation or protection
from side effects performed by configured MCP servers. The prompt prohibits
edits and side-effecting MCP calls. The wrapper reviews from a disposable,
independent local clone created with `git clone --no-local --no-hardlinks` from
the controller repository at its current `HEAD`; the clone does not share Git
objects, refs, configuration, hooks, or worktree metadata with the controller.
The wrapper copies the review
document and optional reference specification into the same relative paths
when either file is untracked or differs from `HEAD`. It rejects symlinked or
out-of-repository document paths. It hashes a manifest containing every path,
entry type, permission mode, symlink target, and regular-file SHA-256 before and
after the review and fails on a difference. The copied snapshot document path,
not the controller path, is passed to `scripts/review-delegate.sh`. A reviewer mutation therefore cannot modify the controller
checkout, although this mechanism cannot prevent effects through configured
MCP servers or outside the disposable workspace. An unchanged snapshot is
removed. A changed snapshot is preserved for diagnosis and its path is returned
with the failure. The external-side-effect risk is accepted for parity with the
working Claude reviewer.

The external review recommendation to remove `--approve-mcps` is deliberately
not adopted because the user selected unchanged reviewer flags. This is a
recorded trust decision, not a claim that prompt instructions provide a
security boundary.

Every initial review and resumed re-review creates a fresh independent clone
from the then-current controller `HEAD` and overlays the then-current declared
documents. The Cursor session ID, document-relative paths, target, lenses, and
latest snapshot outcome are persisted in the reviewer's run-state record.
Cursor resume is required to succeed when the prior clone no longer exists and
the new clone path differs; the real smoke test exercises that condition.
Reviewer transitions are `dispatching -> reviewed`, `reviewed -> dispatching`
for re-review, and `reviewed -> complete` when the controller closes the review
cycle. `dispatching` or `reviewed` may transition to `blocked`. Reviewer records
do not use coder-only integration states.

## Structured Result Contracts

The existing delegate contracts remain authoritative.

Coder wrapper output has this exact field set:

```json
{
  "status": "DONE",
  "run_id": "0123456789abcdef",
  "generation": 4,
  "coder": "cursor-coder",
  "session_id": "provider-session-id",
  "attempts": 0,
  "verified": true,
  "changed": true,
  "commit_id": "",
  "result": "worker summary",
  "verify_output": "verification output",
  "diagnostic": ""
}
```

Reviewer wrapper output has this exact field set:

```json
{
  "status": "REVIEWED",
  "run_id": "0123456789abcdef",
  "generation": 2,
  "reviewer": "cursor-reviewer",
  "session_id": "provider-session-id",
  "target": "spec",
  "lenses": ["backend"],
  "report": "verbatim review",
  "diagnostic": "",
  "snapshot_path": ""
}
```

On review failure the same field set is returned with `status: "BLOCKED"`, an
empty `report`, a non-empty `diagnostic`, and `snapshot_path` set only when a
mutated or otherwise diagnostic snapshot was intentionally preserved. Coder
failure uses the coder field set with `status: "BLOCKED"`, `verified:false`,
and the failure explanation in `diagnostic`; `verify_output` remains reserved
for the verification command's output.

A missing session ID is never success when continuation is part of the role contract. Because `scripts/review-delegate.sh` currently accepts a non-empty report with an empty session ID, the Hermes wrapper performs this additional validation and replaces that result with `status: "BLOCKED"`, exit code 1, and diagnostic `reviewer returned no session id`.

## Failure Policy

The Hermes adapter fails closed for:

- missing model configuration;
- missing Bash 5;
- missing `cursor-agent`;
- Cursor authentication failure;
- invalid or unavailable model identifier;
- missing Cursor session ID;
- worktree preparation failure;
- verification failure after the bounded retry limit;
- an empty verification command or a code result with `verified:false`;
- repository modification during reviewer mode;
- failed fast-forward integration;
- unintegrated worktree commits during cleanup.

The adapter never:

- selects `auto` silently;
- substitutes a different model;
- writes code in the controller checkout;
- treats `cursor-agent status` as proof of usable authentication;
- reports success from the worker's prose alone;
- lets the implementation worker perform final review;
- removes a worktree containing unintegrated commits.

Reviewer mode is not claimed to be a general security sandbox. Its accepted
trust boundary and MCP limitation are documented in the reviewer section.

The Cursor process runs with a minimal environment containing only the
resolved executable `PATH`, `HOME`, `TMPDIR`, locale variables, and basic shell
identity variables, plus the wrapper's generated pool and run identifiers. The
wrapper resolves profile configuration before constructing that environment and
does not forward the Hermes profile environment or API-key variables. Retaining
the real `HOME` for Cursor is required for its file-based authentication and
permits the Cursor process to read user-level credential files.

Verification receives a separate empty `0700` temporary home through an opt-in
`CSC_VERIFY_HOME` path handled by `scripts/code-delegate.sh`; it does not receive
the real `HOME`. The initial version does not support verification commands that
require credentials or user-level configuration. Verification output still passes through the existing
delegate correction prompt and result JSON, so the skill warns that commands
must not print secrets; output is also subject to Hermes terminal redaction.

## Adapter-Parity Contract

Adapter-level workflow duplication is intentional. The repository makes that duplication visible through three mechanisms.

### Automatically loaded maintenance instructions

- `CLAUDE.md` tells Claude Code to inspect adapter parity before changing mapped files.
- `AGENTS.md` tells Hermes and compatible coding agents the same thing.

Both files point to `docs/adapter-parity.md` rather than duplicating the complete parity policy.

### Mapping document

`docs/adapter-parity.md` maps:

```text
commands/implement-plans.md
  <-> hermes/skills/cursor-coder/SKILL.md

agents/coder-delegator.md
  <-> Hermes coder wrapper contract

commands/review.md
  <-> hermes/skills/cursor-reviewer/SKILL.md

agents/reviewer-delegator.md
  <-> Hermes reviewer wrapper contract
```

The document classifies behavior as shared, Claude-specific, or Hermes-specific.

Shared behavior includes:

- worktree safety;
- verification requirements;
- session-ID requirements;
- retry and resume policy;
- controller/worker boundaries;
- rubric selection;
- failure classification;
- integration and cleanup rules.

Claude-specific behavior includes:

- `$ARGUMENTS`;
- `${CLAUDE_PLUGIN_ROOT}`;
- Claude custom-agent dispatch;
- Claude tool allowlists;
- Claude plugin command names.

Hermes-specific behavior includes:

- `${HERMES_SKILL_DIR}`;
- native plugin registration;
- `hermes config get`;
- `skill_view` and Hermes skill invocation;
- Hermes `todo` behavior;
- profile-scoped configuration;
- Hermes terminal and background-process handling.

A maintainer changing a mapped adapter must:

1. inspect its counterpart;
2. classify the change as shared or host-specific;
3. update both adapters for shared behavior;
4. record deliberate divergence in the parity document;
5. run the Claude and Hermes contract tests.

### Per-file parity markers

Each mapped adapter file contains a short marker naming its counterpart and the parity document. The marker does not alter executable workflow semantics.

An automated test verifies that every mapped path exists and carries the expected marker. The test does not compare files byte-for-byte because host-specific instructions intentionally differ. Markers are an awareness mechanism, not proof of semantic equivalence. Shared harness scenarios run against both host adapters where automation is possible, and the release checklist requires Claude and Hermes end-to-end smoke tests for behavior encoded only in prose.

`tests/adapter-contract.json` names the shared scenarios: missing
configuration, authentication failure, missing session ID, empty verification,
failed verification, session resume, dirty controller checkout, concurrent
worktree creation, and cleanup with unintegrated commits. Hermes adapter tests
execute every applicable scenario with mocks. Existing delegate tests continue
to exercise the Claude-used script path. `tests/e2e-smoke.md` gains matching
Claude and Hermes steps identified by the same scenario names for behavior that
cannot be proven from Markdown adapters automatically.

Hermes preserves the existing observability contract. After each completed or
abandoned coder workflow, the controller appends a host-tagged entry to
`docs/cursor-coder/effectiveness-log.md`. After each completed or blocked
document-review workflow, it appends a host-tagged entry to
`docs/cursor-reviewer/effectiveness-log.md`. Reviewer snapshot mutation checks
finish before this intentional controller-side logging occurs.

## Existing Files and Compatibility

The following existing files receive only parity markers; their existing Claude workflow behavior remains unchanged:

```text
commands/implement-plans.md
commands/review.md
agents/coder-delegator.md
agents/reviewer-delegator.md
```

Documentation changes are expected in:

```text
README.md
CHANGELOG.md
```

`Makefile` gains a Hermes manifest variable, synchronized version bumps for both
manifests, a version-equality guard, and the new tests in the release gate.
`.claude-plugin/plugin.json` changes only when a release version is prepared.

The initial implementation leaves these existing runtime files unchanged:

```text
scripts/review-delegate.sh
scripts/probe.sh
scripts/lib/*
.claude-plugin/coders.json
.claude-plugin/reviewers.json
rubrics/*
```

`scripts/worktree.sh` receives only the optional `CSC_RUN_ID` naming extension described above. Tests must prove that the unset path preserves current Claude behavior.
`scripts/code-delegate.sh` receives only optional `CSC_VERIFY_HOME` handling.
`scripts/harness/cursor.sh` receives optional `CSC_STREAM_MAX_BYTES` enforcement
and `CSC_CURSOR_SANDBOX` handling. Each shared script's behavior remains
unchanged when its new variable is unset.

## Installation and Activation

The intended installation flow installs the plugin disabled, configures the
required settings explicitly for the named profile, and only then enables it:

```bash
hermes -p super-dev-codex plugins install sorcush/claude-subagents --no-enable

hermes -p super-dev-codex config set \
  plugins.entries.claude-subagents.settings.coder_model \
  composer-2.5

hermes -p super-dev-codex config set \
  plugins.entries.claude-subagents.settings.reviewer_model \
  gpt-5.6-sol-high

hermes -p super-dev-codex plugins enable claude-subagents
```

A fresh Hermes session is required after plugin enablement so the plugin registry and skill inventory are rebuilt cleanly. The wrapper reports only the active profile identifier: the basename of `HERMES_HOME`, except that the installation root `~/.hermes` maps to `default`. It refuses to run when `HERMES_HOME` is absent. It does not print the full profile path or hard-code `super-dev-codex` into distributable code.

No other Hermes profile is modified.

## Implementation Stages

Implementation proceeds as three independently reviewable stages.

1. **Shared opt-in primitives:** add `CSC_RUN_ID` worktree naming,
   `CSC_STREAM_MAX_BYTES` stream enforcement, opt-in Cursor coder sandboxing,
   and `CSC_VERIFY_HOME`, with legacy-unset regression tests.
2. **Plugin, configuration, wrapper, and reviewer:** add the native manifest,
   skill registration, profile configuration, exact wrapper contracts, durable
   run state, independent review clones, and reviewer smoke tests.
3. **Coder orchestration and recovery:** add ad-hoc and plan-mode instructions,
   Git-control-plane validation, controller commits, correction loops,
   reconciliation, effectiveness logging, and end-to-end coder tests.

Each stage must pass the full existing suite and its new focused tests before
the next stage begins.

## Verification Strategy

### Existing harness regression suite

Run every discovered `tests/test-*.sh` file under the discovered Bash 5 interpreter. The design investigation observed 285 assertions with zero failures; that number is evidence, not a permanent expected count.

### Hermes adapter tests

Add deterministic tests for:

- Hermes manifest validity;
- registration of both qualified skills;
- plugin `config_schema` declarations;
- profile-specific model lookup;
- missing and blank configuration failure;
- Bash 5 discovery and failure;
- temporary pool construction;
- coder and reviewer routing;
- temporary-file cleanup;
- no fallback-model behavior;
- rejection of an empty verification command and `verified:false`;
- rejection of a reviewer result with an empty session ID;
- distinct worktrees for concurrent run IDs and unchanged legacy naming when no run ID is set;
- result-size enforcement and signal cleanup;
- internal Cursor stream-size enforcement;
- recovery-state transitions, atomic writes, corrupt-state refusal, and retention cleanup;
- recovery-state locking, generation compare-and-swap, and crash reconciliation;
- review snapshot fidelity for content, mode, symlink, and type changes;
- proof that review clones do not share their Git directory or object storage with the controller repository;
- pre/post coder Git-control-plane validation and refusal of autonomous commits;
- direct controller-checkout write detection and reachable-object corruption detection;
- adapter-parity mappings and markers.

Stream-limit tests launch a mock Cursor process with a descendant writer and
prove that overflow terminates the complete process group, removes the partial
stream file, emits a structured failure, and leaves behavior unchanged when
`CSC_STREAM_MAX_BYTES` is unset.

Plugin validation uses the exact command:

```bash
hermes -p super-dev-codex plugins doctor . --ci
```

It must exit 0 and report no validation errors. After installation, the same
check runs against the installed ID: `hermes -p super-dev-codex plugins doctor
claude-subagents --ci`.

### Claude regression checks

1. Run `claude plugin validate .`.
2. Capture the Claude plugin component inventory before and after the change.
3. Confirm that no Hermes skill appears as a Claude skill or command.
4. Run every existing harness test.
5. Run shared adapter contract scenarios through both host paths where automation is possible.
6. Run the existing Claude smoke workflow when the installed Claude environment permits it, including worktree creation, verification, commit, and cleanup.

### Real Hermes smoke checks

1. Install the plugin disabled in `super-dev-codex`.
2. Configure both model settings.
3. Enable the plugin and start a fresh Hermes session.
4. Load both qualified skills.
5. Run real coder and reviewer probes.
6. Run a read-only specification review and verify no repository changes.
7. Run a coder task on a throwaway feature branch.
8. Verify that the controller checkout remains unchanged before integration.
9. Commit, review, and independently verify the worktree result.
10. Fast-forward the reviewed commit.
11. Resume the same Cursor session for one correction.
12. Confirm safe worktree cleanup.
13. Verify that no other Hermes profile changed.

## Acceptance Criteria

The implementation is complete only when:

1. Claude plugin validation succeeds.
2. Claude's existing component inventory is unchanged.
3. All discovered harness and adapter tests pass; report the observed assertion count as evidence rather than asserting a fixed count.
4. Hermes plugin validation succeeds.
5. Both qualified skills load in a fresh Hermes session.
6. Both profile configuration values resolve through the wrapper.
7. Both real Cursor probes return `READY`.
8. The external reviewer phase produces no worker-induced change to the
   controller repository; any later effectiveness-log change is an intentional
   and separately verified controller write.
9. Coder mode changes only its isolated worktree before integration.
10. The Hermes controller independently verifies the coder result.
11. A resumed correction uses the original Cursor session.
12. Cleanup refuses to discard unintegrated commits.
13. Only `super-dev-codex` receives configuration changes.
14. Adapter-parity tests pass.
15. Empty verification commands, `verified:false`, and missing reviewer session IDs fail closed.
16. Concurrent Hermes runs receive distinct worktrees while the legacy Claude worktree path remains unchanged without `CSC_RUN_ID`.
17. Both manifests carry the same version and release tooling refuses version drift.
18. Reviewer resume succeeds in a fresh independent clone after the original
    review clone has been removed.
19. Coder dispatch rejects unauthorized ref, configuration, hook, branch, or
    worktree-registration changes before integration.

## Risks and Mitigations

### Claude adapter regression

**Risk:** New repository files are accidentally discovered as Claude components.  
**Mitigation:** Keep Hermes skills below `hermes/skills/`, validate the Claude plugin, and compare component inventories.

### Adapter drift

**Risk:** A future maintainer changes one host workflow but not the other.  
**Mitigation:** Root context files, an explicit parity map, per-file markers, shared executable contract scenarios, and host-specific smoke tests. The design does not claim that static markers prove semantic equivalence.

### Model retirement

**Risk:** A configured Cursor model disappears from the live catalog.  
**Mitigation:** Run a real model probe before dispatch and fail with `bad-model`; never silently substitute another model.

### Bash mismatch

**Risk:** Hermes resolves Apple Bash 3.2 while the harness requires Bash 5.  
**Mitigation:** Hermes-only interpreter discovery and absolute Bash invocation.

### Configuration drift

**Risk:** A plugin release renames settings and strands existing profile values.  
**Mitigation:** Treat `coder_model` and `reviewer_model` as stable public configuration keys; require explicit migration for any future rename.

### Worker self-approval

**Risk:** A Cursor worker reports success for incorrect or out-of-scope work.  
**Mitigation:** The Hermes controller inspects the actual diff and reruns verification before integration.

### Reviewer MCP side effects

**Risk:** The retained `--approve-mcps` flag allows a configured MCP server to perform effects outside the repository even while Cursor is in documented ask mode.  
**Mitigation:** Document this as an accepted trust boundary, prohibit side-effecting MCP use in the review prompt, and verify repository state before and after. The initial version does not claim stronger containment.

### Concurrent runs

**Risk:** Deterministic worktree names can mix two active sessions.  
**Mitigation:** Use optional run-scoped worktree names for Hermes while preserving the current unset behavior for Claude.

### Partial plan completion

**Risk:** A later task can fail after reviewed commits were integrated.  
**Mitigation:** Keep those reviewed commits, preserve the failed worktree, perform no automatic revert, and report exact recovery commands.

### Manifest version drift

**Risk:** Claude and Hermes manifests advertise different releases.  
**Mitigation:** Make `.claude-plugin/plugin.json` and `plugin.yaml` versions equal, update Makefile bump targets to change both through a validated synchronized operation, and add version-sync and partial-write failure tests.
