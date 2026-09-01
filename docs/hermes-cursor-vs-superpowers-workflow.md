# Hermes Cursor Plugin vs. the Superpowers Workflow

## Purpose

This document explains how the Hermes workflows provided by this repository
relate to the standard Superpowers development methodology.

Sources:

- [Superpowers: The Basic Workflow](https://github.com/obra/superpowers/tree/main#the-basic-workflow)
- `hermes/skills/cursor-coder/SKILL.md`
- `hermes/skills/cursor-reviewer/SKILL.md`
- `hermes/scripts/dispatch.py`
- `docs/adapter-parity.md`

## Executive summary

Superpowers and this plugin solve different parts of the development process.

Superpowers defines the methodology:

```text
understand → design → plan → implement with TDD → review → finish the branch
```

The plugin defines a guarded execution adapter for two roles:

- Cursor implements code as an external worker.
- Cursor reviews specifications and implementation plans as an external reviewer.

The plugin does not replace brainstorming, design approval, implementation
planning, TDD, task review, or branch completion. It replaces the
implementation-worker boundary and adds stronger execution isolation, durable
state, session continuation, Git protection, and recovery behavior.

The combined model is:

```text
Superpowers decides how development proceeds.
Hermes controls the run and repository lifecycle.
Cursor performs bounded implementation or document-review work.
```

## Workflow overview

### Standard Superpowers

```text
Human request
  → brainstorming
  → approved design
  → isolated feature worktree
  → implementation plan
  → fresh implementer subagent
  → TDD, tests, commit, self-review
  → fresh task reviewer
  → correction loop
  → next task
  → final branch review
  → merge, PR, or keep branch
```

### Superpowers with `cursor-coder`

```text
Human request
  → brainstorming
  → approved design
  → isolated controller feature worktree
  → implementation plan
  → Hermes creates a run and Cursor worker worktree
  → Cursor edits and runs harness verification
  → Hermes inspects and independently verifies
  → Hermes creates the local commit
  → Superpowers/Hermes review gate
  → same Cursor session corrects findings
  → Hermes fast-forwards the reviewed commit to the feature branch
  → next task with a new Cursor session
  → final branch review
  → merge, PR, or keep branch
```

## Stage-by-stage comparison

| Stage | Standard Superpowers | Hermes Cursor plugin | Result when combined |
|---|---|---|---|
| Request refinement | `brainstorming` asks questions and explores alternatives | No replacement | Superpowers remains authoritative |
| Design approval | Design is presented in sections and approved by the user | Optional external `cursor-reviewer` review | Cursor can challenge the design, but the user still approves it |
| Initial workspace | `using-git-worktrees` creates or validates a feature workspace | Creates an additional run-scoped Cursor worktree | Controller and worker edits are separated |
| Planning | `writing-plans` creates explicit implementation tasks | Consumes the approved plan | Plan format and authority remain unchanged |
| Implementer | Fresh general-purpose subagent per task | External `cursor-agent` process using the configured coder model | Cursor replaces only the implementer boundary |
| TDD | `test-driven-development` requires RED–GREEN–REFACTOR | Requires an explicit executable verification command | Methodology plus mechanical verification |
| Implementation commit | Implementer normally commits its task | Cursor is forbidden to commit; Hermes commits after verification | Commit authority moves to the controller |
| Task review | Fresh reviewer checks specification compliance and code quality | Hermes preserves the review gate; Cursor implementer cannot self-approve | Review remains independent from implementation |
| Correction | Early rounds resume the original implementer | Resume the exact Cursor session with zero internal retries | Cursor keeps task context without another Hermes agent layer |
| Integration | Task commits accumulate on the feature branch | Reviewed worker commits are fast-forwarded into the feature branch | Only reviewed commits reach the feature branch |
| Final branch action | `finishing-a-development-branch` offers merge, PR, or keep | No replacement | Superpowers still owns the final user decision |
| Recovery | Git history and the ignored SDD ledger | Durable JSON state, locks, generations, session IDs, and Git fingerprints | Recovery survives process and context loss |

## What remains unchanged from Superpowers

### 1. Brainstorming and scope classification

Superpowers still activates before implementation work.

It classifies the request as a spike, bounded change, or architectural change.
It gathers requirements, presents alternatives, and waits for explicit user
approval before implementation.

The plugin has no brainstorming workflow. Calling `cursor-coder` does not make
requirements discovery optional.

### 2. Design and specification

For architectural work, Superpowers still produces the design specification.
The specification remains the binding authority for the implementation plan and
review findings.

The plugin can add an external Cursor review of the specification, but that
review does not replace:

- design self-review;
- user review;
- user approval.

### 3. Implementation planning

Superpowers still creates the implementation plan through `writing-plans`.
The plan defines file paths, interfaces, test steps, verification commands, and
commit boundaries.

The plugin consumes that plan. It does not introduce a competing plan format.

### 4. TDD methodology

Superpowers still requires RED–GREEN–REFACTOR:

1. Write a failing test.
2. Observe the expected failure.
3. Write the minimum implementation.
4. Observe the test passing.
5. Refactor while green.

The plugin can enforce that the final verification command succeeds. It cannot
prove from the final repository state that every test was written before its
implementation. The TDD sequence therefore remains a methodological Superpowers
requirement.

### 5. Review gates

Superpowers still requires specification-compliance and code-quality review.
Critical and Important findings block normal progression until they are fixed or
adjudicated through the bounded review process.

The external Cursor implementation worker never approves its own changes.

### 6. Branch completion

After implementation and final review, Superpowers still runs
`finishing-a-development-branch`.

The user still chooses whether to:

1. merge locally;
2. push and create a pull request;
3. keep the branch.

The plugin does not push, publish, merge remotely, release, or rewrite history
without an explicit user request.

## What the plugin changes

### 1. External Cursor replaces the implementation subagent

Standard Subagent-Driven Development dispatches a fresh coding subagent for a
task. The plugin invokes `cursor-agent` directly through the existing shell
harness.

```text
Hermes controller
  → cursor-coder skill
    → Python adapter
      → Bash delegate and harness
        → Cursor Agent CLI
```

Hermes `delegate_task` is not part of this path. Adding a Hermes child around
Cursor would add another model call without improving the external worker
contract.

The coder model is read from the active Hermes profile:

```text
plugins.entries.claude-subagents.settings.coder_model
```

The adapter has no silent model fallback.

### 2. The plugin adds a second isolation boundary

Superpowers normally creates an isolated feature worktree for the controller.
The plugin creates a second, run-scoped worktree for Cursor.

```text
main checkout
  └── controller feature worktree
        └── Cursor run-scoped worker worktree
```

The controller feature worktree contains reviewed project history. Cursor edits
only the worker worktree.

A stable run ID identifies the worker branch and worktree. The adapter stores the
original feature-branch identity so two branch names that sanitize to the same
filesystem slug cannot share or delete each other's worktree.

### 3. Cursor does not own commits

A standard Superpowers implementer normally implements, tests, commits, and
self-reviews its task.

The plugin intentionally removes commit authority from Cursor. Cursor may edit
files and run commands in its worktree. Cursor must not:

- create commits;
- switch branches;
- update refs;
- change Git configuration;
- change effective hooks;
- alter worktree registration;
- write into the controller checkout;
- push or publish anything.

After accepting the result, Hermes creates the local worktree commit.

### 4. Verification happens at two levels

The deterministic delegate runs the explicit verification command inside the
Cursor worktree.

Hermes then independently:

- inspects the changed-file scope;
- reads the actual diff;
- reruns verification;
- compares the controller checkout with its original manifest;
- compares refs, configuration, hooks, and worktree metadata;
- runs `git fsck --full`.

A successful Cursor message is evidence, not acceptance.

### 5. Verification uses a disposable home

The verification command runs with a minimal environment and a physical,
disposable home directory.

This prevents verification from implicitly using the user's credential files or
shell configuration. Verification commands that require credentials are not
supported by the initial adapter contract.

Cursor itself retains its normal user home because Cursor authentication is
managed by the Cursor CLI.

### 6. Corrections resume the same Cursor session

If review finds a problem, Hermes resumes the exact Cursor session used for that
task.

A correction keeps:

- the run ID;
- the worktree;
- the task's Cursor session ID;
- the latest state generation.

Correction dispatches use zero internal verification retries. Hermes diagnoses
the failure before deciding what Cursor should change.

### 7. Each plan task gets a fresh Cursor session

A multi-task plan keeps one run ID and one worker worktree. Each newly integrated
task starts a fresh Cursor session and fresh task budgets.

Corrections within a task reuse that task's session.

This maps Superpowers' “fresh implementer per task” principle onto Cursor's own
session model.

### 8. Reviewed tasks are integrated immediately

After task review succeeds:

1. Hermes records the approved commit as pending integration.
2. Hermes fast-forwards the controller feature branch to that commit.
3. The adapter reconciles the real Git ancestry.
4. The task state becomes integrated.

If a later task fails, earlier reviewed task commits remain on the feature
branch. The plugin does not automatically revert them.

### 9. The plugin adds durable recovery state

Superpowers uses Git history and an ignored task ledger as its main recovery
map. The plugin additionally writes machine-readable state under:

```text
$HERMES_HOME/claude-subagents/runs/<run-id>.json
```

The state records:

- repository and worktree identity;
- feature and work branches;
- role and lifecycle state;
- generation number;
- Cursor session ID;
- pending, integrated, and unintegrated commits;
- verification and protected-state fingerprints;
- per-task execution budgets;
- failure diagnostics.

Every mutation requires the latest generation. A stale controller cannot update
a run after another controller has advanced it.

### 10. The plugin adds Git control-plane protection

A linked worktree shares more than source files with the controller repository.
It shares objects, refs, configuration, hooks, and worktree registration.

The adapter therefore records and compares protected Git state before and after
Cursor runs. It includes the effective hooks directory selected by
`core.hooksPath`.

A protected-state mismatch blocks the run. A blocked run cannot redefine the
already-modified repository as its new clean baseline.

## Task loop comparison

### Standard Subagent-Driven Development task

```text
fresh implementer
  → implement with TDD
  → run tests
  → commit
  → self-review
  → fresh task reviewer
  → fix and re-review if needed
```

### Plugin-backed task

```text
Cursor worker
  → edit isolated worker worktree
  → harness verification
  → return structured result and session ID

Hermes controller
  → inspect diff
  → independently verify
  → verify protected Git state
  → create local commit
  → apply specification and quality review gates

If findings exist
  → resume same Cursor session
  → verify again
  → create correction commit
  → re-review

If approved
  → fast-forward feature branch
```

## Plan-mode comparison

### Superpowers

Superpowers executes tasks serially. Each task receives a fresh implementer and
a task-scoped review. A whole-branch review runs after all tasks.

### Plugin

The plugin preserves that structure while changing worker mechanics:

```text
Task 1
  → fresh Cursor session
  → verification
  → Hermes commit and review
  → fast-forward to feature branch

Task 2
  → same run and worktree
  → fresh Cursor session and budgets
  → verification
  → Hermes commit and review
  → fast-forward

Later task fails
  → earlier integrated commits remain
  → failed worker worktree and state remain for recovery
```

The adapter enforces per-task ceilings:

- nine Cursor calls;
- five controller review rounds;
- five active worker hours.

A new run cannot be created merely to bypass an exhausted task budget.

## Review-flow comparison

### Superpowers review

Superpowers task review checks implementation work against the plan and code
quality expectations. The final review checks the whole branch.

### Plugin `cursor-reviewer`

The plugin's dedicated reviewer is narrower. It accepts only:

- design specifications;
- implementation plans.

It is not the implementation code reviewer used by `cursor-coder`.

For each document review, Hermes:

1. validates the repository and declared documents;
2. creates a disposable clone with `--no-local --no-hardlinks`;
3. overlays only the declared documents;
4. records a filesystem manifest;
5. probes the configured reviewer model;
6. runs Cursor in ask mode;
7. requires a non-empty report and session ID;
8. checks that the clone was not modified;
9. removes an unchanged clone or preserves a changed clone as evidence.

A re-review creates a new clone but resumes the same Cursor session.

The external report is triaged through Superpowers'
`receiving-code-review` workflow. It does not replace user approval of a design.

## Review responsibility

| Review | Standard Superpowers | Plugin behavior |
|---|---|---|
| Design discussion | Brainstorming and user approval | Unchanged |
| Specification external review | Optional | `cursor-reviewer` can provide it |
| Plan external review | Optional | `cursor-reviewer` can provide it |
| Implementation self-review | Implementer performs it | Cursor may assess its work, but cannot approve it |
| Task specification review | Fresh task reviewer | Preserved as a Hermes/Superpowers review gate |
| Task code-quality review | Fresh task reviewer | Preserved as a Hermes/Superpowers review gate |
| Final whole-branch review | Most capable reviewer | Unchanged |
| Final merge/PR decision | User through branch-finishing workflow | Unchanged |

## Failure and recovery comparison

### Superpowers

Superpowers preserves progress through:

- task commits;
- the feature branch;
- the SDD ledger;
- task reports and review packages.

### Plugin

The plugin preserves all of the above and adds:

- a stable run ID;
- a stable Cursor session ID per task;
- generation-checked state updates;
- process locks;
- crash reconciliation against real Git ancestry;
- protected repository fingerprints;
- retained worker worktrees after failure;
- idempotent completion tombstones.

The key rule is:

> Failure preserves evidence.

Cleanup refuses to remove work containing unintegrated commits. A refusal leaves
the lifecycle state reviewable so the controller can integrate the preserved
commit later.

## Reviewer trust boundary

Cursor reviewer mode uses `--mode ask`, but the shared invocation retains
`--approve-mcps` by explicit design decision.

Ask mode prevents normal repository editing through Cursor's native edit tools.
It does not provide operating-system isolation from configured MCP servers.
Independent clones and mutation checks protect the repository, but they cannot
prevent external side effects performed through an approved MCP server.

## Recommended combined workflow

For architectural work:

1. Use Superpowers brainstorming.
2. Approve and commit the design.
3. Optionally use `cursor-reviewer` for an external specification review.
4. Use Superpowers `writing-plans`.
5. Optionally use `cursor-reviewer` for an external plan review.
6. Approve the implementation plan.
7. Use `cursor-coder` in plan mode.
8. Preserve Superpowers task and final review gates.
9. Use `finishing-a-development-branch` for the merge, PR, or keep decision.

For bounded work:

1. Use the short Superpowers design and approval gate.
2. Supply one bounded task and one verification command to `cursor-coder`.
3. Let Hermes verify, commit, review, fast-forward, and clean up.
4. Finish the feature branch through Superpowers.

## Responsibility matrix

| Responsibility | Owner |
|---|---|
| Clarify user intent | Superpowers/Hermes |
| Approve design | User |
| Write specification | Superpowers/Hermes |
| Write implementation plan | Superpowers/Hermes |
| Select configured external model | Plugin adapter |
| Edit implementation files | Cursor |
| Run immediate verification | Delegate harness |
| Inspect actual diff | Hermes controller |
| Independently rerun verification | Hermes controller |
| Create local commits | Hermes controller |
| Apply task review gates | Superpowers/Hermes |
| Correct review findings | Same Cursor task session |
| Fast-forward reviewed commits | Hermes controller |
| Choose merge, PR, or keep | User |
| Push, publish, or release | Only after explicit user authorization |

## Bottom line

Superpowers is the development methodology. The plugin is a guarded Cursor
execution backend for that methodology.

The plugin adds stronger isolation, session continuity, deterministic
verification, Git control-plane protection, and crash recovery. It intentionally
leaves requirements discovery, planning quality, TDD discipline, independent
review, and final integration decisions under Superpowers and the Hermes
controller.
