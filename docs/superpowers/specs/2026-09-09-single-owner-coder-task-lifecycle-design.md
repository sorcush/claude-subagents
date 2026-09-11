# Single-owner coder task lifecycle

Date: 2026-09-09
Status: approved by the user for implementation planning
Issue: https://github.com/sorcush/claude-subagents/issues/5

## Goal

Make it impossible for two delegated coding tasks to write in the same worktree at
the same time through the supported `/implement-plans` workflow.

Today the controller starts a `coder-delegator` subagent, that subagent starts the
external coder, and the subagent later reports completion. The controller treats the
report as the task boundary. A subagent can report `DONE` or be shown as completed by
the host while it or a process it launched is still alive. The controller can then
start the next task in the same worktree. A late process from the earlier task can
change or hide the later task's unfinished work.

After this change, one synchronous script owns the complete write lifecycle for a
task: lock, coder execution, verification, commit, final worktree inspection, and
reporting. The controller does not start the next task until that script has exited
and released ownership. If the system cannot prove the writer stopped, it preserves
the worktree and stops the plan rather than admitting another writer.

## Success criteria

The change is complete when all of these are true:

1. `/implement-plans` never dispatches `coder-delegator` for implementation or fix
   rounds. It invokes the coder lifecycle synchronously and uses the external coder's
   session id directly for continuation.
2. Exactly one lifecycle invocation can own a given coder worktree at a time. A
   second invocation fails before it starts a coder or changes Git state.
3. A success result is produced only after the coder process group has exited,
   verification has completed, the resulting changes have been committed when
   needed, and the worktree is clean.
4. A timeout stops the whole writer process group. If process termination cannot be
   confirmed, the worktree remains quarantined and no later task may use it.
5. A failed or timed-out task never discards, stashes, resets, or cleans its partial
   work. The same task can be resumed deliberately; a different task cannot take over
   a dirty or quarantined worktree.
6. Completion is derived from observed process, verification, and Git state. Text
   such as `DONE`, test counts, or other claims in the coder's answer cannot by itself
   make the lifecycle succeed.
7. Every successful result includes a real coder session id, the commit id or an
   explicit no-change result, the changed paths, verification results, and a clean
   final worktree state.
8. Verification accepts multiple separate shell commands. Each command is validated
   before the coder starts, runs in the coder worktree, and is reported separately.
   Human labels such as `server: npm test` are rejected before execution.
9. Deterministic tests reproduce the original overlap risk and prove the second
   writer is refused, hanging writers are contained, partial work is preserved, and
   malformed verification input never reaches the coder.

## Scope

In scope:

- The `/implement-plans` controller workflow for implementation and fix rounds.
- The coder lifecycle script, including Git validation, locking, verification,
  committing, and result reporting.
- Process-group completion and timeout handling for edit-capable coder harnesses.
- Persistent recovery state for interrupted or blocked coding tasks.
- Removal of `coder-delegator` from the supported write path.
- Documentation and automated/manual tests describing the new behavior.

Out of scope:

- Changing the reviewer workflow. Reviewer delegates remain read-only subagents.
- Changing model pools, model selection, or harness output parsing except where
  writer-lifecycle guarantees require it.
- Giving every plan task a separate Git worktree. The existing worktree is reused
  serially across tasks.
- Fixing the host application's stale or misleading subagent rows.
- Detecting arbitrary processes that deliberately detach into a new operating-system
  session. Supported harnesses must keep all writer descendants in the process group
  created for the invocation; escaping that group is a harness defect.
- Protecting the worktree from commands started manually outside the plugin.

## Chosen approach

The controller invokes the coder lifecycle script directly and waits for it to exit.
The intermediate `coder-delegator` subagent is removed from implementation dispatch.

This is preferred over strengthening the subagent's written instructions. The current
subagent already requires a session id and a structured completion report, but the
reported incident shows that a model can ignore that contract. The subagent also has
general shell access because it must invoke the script and commit, so a still-running
subagent can bypass a cooperative lock or run Git cleanup after reporting.

Direct synchronous ownership removes that ambiguous boundary. The script, not model
text, decides when the task is complete. The script does not return success while a
writer is live.

## Alternatives considered

### Keep the subagent and add controller checks

The controller could validate the completion report, list subagents, inspect running
processes, and take a worktree lock before the next dispatch.

This is useful defense in depth but is not an ownership boundary. A shell-capable
subagent that remains alive can ignore the lock and change the worktree directly.
Host-reported terminal state was also part of the observed failure, so treating it as
proof would preserve the original weakness.

### Create a new worktree for every task

Per-task worktrees would contain most damage from a lingering task because the next
task would use another folder. They would also add branch management, dependency-copy
cost, cleanup state, and merge handling to every task. They do not prevent a worker
from changing shared Git references.

This remains a possible future defense, but it is not required once the supported
write lifecycle has one synchronous owner.

## Lifecycle model

Each invocation is in exactly one of these states:

1. **Validating** — validate all arguments and verification commands without changing
   the worktree.
2. **Owning** — acquire exclusive ownership for the target worktree and inspect its
   recovery state.
3. **Running** — start or resume the coder in a dedicated process group.
4. **Verifying** — run every verification command in order inside the worktree.
5. **Retrying** — resume the same coder session with the failed verification output,
   within the configured attempt limit, then rerun the complete verification list.
6. **Committing** — confirm the branch and starting commit are still valid, stage the
   task's resulting changes, commit them, and inspect the final worktree.
7. **Complete** — emit a successful result and release ownership while exiting.
8. **Recoverable blocked** — the writer is confirmed stopped, but the task did not
   complete. Preserve partial work and recovery identity, emit a blocked result, and
   release only the live lock. No different task may use the worktree.
9. **Quarantined** — writer termination or worktree ownership cannot be proven.
   Preserve the lock and all files. Emit the best available diagnostic and stop the
   plan. Recovery requires an explicit safety check; normal dispatch cannot override
   quarantine.

Only `Complete` permits the controller to review and then advance to another task.
Neither kind of blocked result permits the next task to start.

## Worktree ownership

### Live lock

The lifecycle script acquires an atomic lock before it launches a coder, runs
verification, or changes Git state. The lock belongs to the linked worktree's private
Git metadata directory, so it is outside the tracked working files and distinct for
each linked worktree.

The lock records enough diagnostics to identify its owner: lifecycle id, operating
system process id, process-group id once known, start time, coder key, and worktree.
The session id is added after the coder announces it.

The lock is held through coder execution, all retries, verification, committing, the
final status check, and result emission. Normal exit and handled interruption release
it only after every writer process is confirmed stopped. A second invocation that
finds a live lock returns a blocked result without invoking a coder or changing the
worktree.

The implementation must use a locking operation that is atomic on both macOS and
Linux. It must not depend on `flock`, which is not present on macOS by default.

### Recovery record

The live lock is not the only state. A separate recovery record remains when a task
ends with partial work or when the lifecycle is interrupted. It contains:

- a generated lifecycle id;
- the coder key and coder session id, when available;
- the worktree and work-branch identity;
- the commit at which the invocation started;
- the last branch and commit observed before the lifecycle stopped;
- whether the last writer was confirmed stopped;
- the changed-path snapshot;
- the last verification failure and attempt count.

An initial task invocation requires no recovery record and a clean worktree. If a
recovery record exists, only an explicit resume carrying the matching lifecycle id
may continue that work. A new task is refused even when the worktree happens to look
clean, because a prior task may still own unfinished state.

A successful no-change or committed result removes the recovery record. A recoverable
blocked result keeps the record but releases the live lock after process termination
is confirmed. A quarantined result keeps both the recovery record and quarantine
marker.

The plugin never automatically removes a quarantine after an unhandled crash. The
diagnostic tells the user what remains and requires explicit recovery after checking
that no writer process is alive. This deliberately trades convenience for protection
of unfinished work.

### Recovery operation

The lifecycle script provides an explicit recovery operation for a blocked or
quarantined worktree. It requires the worktree path and matching lifecycle id. It:

1. atomically excludes normal task admission while recovery is checked;
2. confirms that the recorded owner process and process group have no live members;
3. confirms that the worktree is still the recorded linked worktree and that its
   branch and commit match the last state recorded when the lifecycle stopped,
   allowing only the recorded uncommitted task changes;
4. converts quarantine into recoverable blocked state and removes a stale live lock;
5. preserves every working file and commit.

Recovery refuses when liveness or worktree identity is uncertain. It has no force
mode. After recovery succeeds, the controller resumes the same task with both the
matching lifecycle id and coder session id. If no usable coder session id was captured,
the controller stops and asks the user how to handle the preserved changes; it does
not start a new task.

An unexpected branch switch or coder-created commit is quarantined even after the
writer stops. The recovery operation does not approve that Git change. The user must
inspect and either retain or undo it, restore the expected branch state, and then run
recovery again. The plugin never makes that decision automatically.

## Process completion and hangs

Every edit-capable coder invocation runs in its own process group. The lifecycle waits
for that group, not merely for a final line of model output.

On normal completion:

1. The harness receives the terminal tool result.
2. The direct child exits.
3. The lifecycle confirms that no process remains in the invocation's process group.
4. Only then may verification or completion continue.

If descendants remain after the direct child exits, the lifecycle treats them as
lingering writers. It requests graceful termination, waits for the bounded grace
period, then forcibly terminates the remaining group. The task is blocked rather than
successful because the tool violated the process-lifecycle contract.

On timeout:

1. The lifecycle records that the deadline was reached.
2. It sends graceful termination to the whole writer process group.
3. After the bounded grace period, it forcibly terminates remaining members.
4. It confirms the process group is empty.
5. If confirmed, it records a recoverable blocked state, preserves all partial files,
   and returns the real session id when one was announced.
6. If not confirmed, it quarantines the worktree and does not release ownership for
   another task.

Verification commands are also bounded. A verification timeout blocks the task,
preserves its changes, and follows the same process-group cleanup rule.

The controller never responds to a hang by starting another task in the same
worktree. A hang costs progress, not another task's work.

## Git ownership and commit behavior

The lifecycle script becomes the only supported component that changes Git state for
a delegated task. The external coder is instructed to edit files and run focused
checks, but not to commit, stash, switch branches, reset, clean, or restore files.
These instructions reduce accidental interference; the safety boundary does not rely
on obedience to them.

After acquiring ownership, the script records the current branch, current commit, and
worktree state. It refuses a fresh task when:

- the target is not a linked worktree;
- the branch is `main` or `master`;
- the worktree is dirty without a matching recovery lifecycle;
- another lifecycle owns or quarantines the worktree.

Before committing, the script confirms that the branch and starting commit were not
changed by the coder. An unexpected branch switch or commit blocks the task and
preserves the resulting state for inspection. The script does not reset or rewrite
the coder's Git actions automatically.

When verification succeeds:

- If no tracked or untracked task changes exist, the script reports an explicit
  no-change success and creates no commit.
- If changes exist, the script stages them, commits with the controller-supplied task
  message, captures the commit id and changed paths, and then requires a clean
  worktree.
- If committing fails, or a commit hook leaves additional changes, the result is
  blocked. Existing commits and files are preserved and reported.

The controller never runs Git write operations in the coder worktree while the
lifecycle script is active. After a successful task review, it may fast-forward the
feature branch as before.

## Verification input and execution

The coder lifecycle accepts `--verify-cmd` more than once. Every occurrence is one
complete runnable shell command. The current single-command form remains valid.

Before acquiring the worktree lock or launching a coder, the script validates the
complete list:

- At least one `--verify-cmd` argument is required. A single empty value continues to
  mean that the task explicitly has no automated verification.
- An empty command cannot be mixed with non-empty commands.
- Every non-empty command must pass shell syntax validation.
- A command whose leading token ends in `:` is rejected as a likely human-readable
  label rather than an executable command. The diagnostic explains that labels must
  be removed and separate commands passed separately.

Commands run in the order supplied, each inside the coder worktree. The first failure
ends that verification pass and its command, exit status, and output are sent to the
same coder session. After the coder attempts a fix, the full list runs again from the
first command so an earlier passing check cannot silently regress.

The configured retry count counts coder fix attempts, not verification commands.
Every verification command has a bounded runtime. The result reports every command
that ran, its outcome, and its captured output. The script executes the supplied
command as a shell command without using nested evaluation.

## Controller workflow

`/implement-plans` keeps the existing plan loading, coder menu, probe, task review,
fix loop, fast-forward, worktree cleanup, and final branch-finishing behavior. Its
implementation step changes as follows:

1. Select and probe the coder as before.
2. Prepare the task brief and a concise commit message.
3. Invoke the coder lifecycle directly and synchronously for the chosen worktree.
4. Treat operating-system exit plus structured output as one result. A zero exit with
   malformed output, a nonzero exit with success output, or a missing required field
   is blocked.
5. Accept success only when the result contains a real session id, successful or
   explicitly absent verification, a commit id or explicit no-change state, changed
   paths, and a clean final worktree.
6. Review the committed change. Model-written claims in the result are context, not
   evidence.
7. For review fixes, invoke the lifecycle directly again with the same coder session
   id and the review findings. No intermediate subagent is resumed or created.
8. Fast-forward only after the review passes and no recovery or quarantine state
   remains.
9. Before starting the next task, confirm the previous lifecycle command exited, no
   ownership record blocks admission, and the worktree is clean.

The controller does not use host subagent status to decide whether a coder stopped,
because implementation no longer has an in-session coder subagent. Read-only review
subagents do not share the write lifecycle.

A clean review-fix round may start a new lifecycle while continuing the prior coder
session. Resuming dirty work from a blocked invocation additionally requires the
matching lifecycle id from its recovery record. A session id alone never grants
ownership of a dirty worktree.

## Result contract

The lifecycle continues to print exactly one JSON object on standard output and sends
progress to standard error. The result contains:

- `status`: `DONE` or `BLOCKED`;
- `coder`: the selected coder key;
- `session_id`: the external coder session id;
- `lifecycle_id`: the identity required for recovery when work remains;
- `attempts`: coder fix attempts used by this invocation;
- `verification_mode`: `commands` or `none`;
- `verification`: an ordered list of commands that ran, with exit status, timeout
  state, and output;
- `verified`: true only when every supplied verification command passed;
- `changed`: whether the task produced changes relative to its starting commit;
- `commit_id`: the created commit, or an empty value for no-change success;
- `files_changed`: the task's changed paths;
- `worktree_clean`: the final observed cleanliness state;
- `writer_stopped`: whether the complete writer process group was confirmed stopped;
- `result`: the coder's final text, retained as untrusted context;
- `diagnostic`: the lifecycle's own failure explanation.

`DONE` requires `writer_stopped:true`, `worktree_clean:true`, a non-empty
`session_id`, and either `verified:true` or `verification_mode:"none"`. If
`changed:true`, `DONE` also requires a non-empty `commit_id`. Any contradiction is a
script defect and the controller treats it as blocked.

`BLOCKED` reports the best available session id and state without claiming cleanup or
verification that did not occur. A dirty blocked worktree is expected and recoverable;
the controller must not advance to another task.

## Removal and compatibility

`agents/coder-delegator.md` is removed so the unsafe write path is not advertised or
accidentally reused. `reviewer-delegator` remains unchanged.

The public `/implement-plans <plan-path>` command, coder selection menu, model pools,
worktree layout, review gates, and feature-branch fast-forward behavior remain the
same. Users see different behavior only on lifecycle boundaries and failures:

- successful tasks wait for confirmed process exit before review begins;
- malformed verification is rejected immediately;
- a concurrent task is refused;
- hangs stop safely and preserve partial work;
- blocked or quarantined work requires recovery before a later task can start.

The direct script interface remains compatible with one existing `--verify-cmd`.
Multiple occurrences are additive. The new commit-message and lifecycle/recovery
arguments are internal to the plugin workflow and must be documented in the script's
usage output. Every task or fix invocation receives a non-empty, single-line
`--commit-message`. A dirty blocked invocation is resumed with both `--session` and
`--lifecycle-id`. The recovery operation accepts `recover`, `--cwd`, and the matching
`--lifecycle-id`; it never starts a coder.

Exit status remains part of the contract: success exits 0, a valid `BLOCKED` lifecycle
result exits 1, and invalid caller input exits 2. The controller requires the exit
status and JSON status to agree.

## Error handling

| Situation | Required behavior |
|---|---|
| Another lifecycle holds the worktree | Return `BLOCKED` before coder execution; report the owner metadata. |
| A stale or quarantined ownership record exists | Refuse a fresh task; preserve the record and explain explicit recovery. |
| Recovery finds any recorded writer still alive | Refuse and keep quarantine unchanged. |
| Recovery proves the writer stopped | Remove only the stale live lock, retain partial files, and permit a matching resume. |
| Fresh task starts in a dirty worktree | Refuse before coder execution. |
| Resume identity does not match the recovery record | Refuse without changing files or the record. |
| Verification input is missing, contradictory, malformed, or syntactically invalid | Exit with an input error before coder execution or locking. |
| Coder returns no session id | Return `BLOCKED`; preserve observed changes. |
| Coder reports success text but exits unsuccessfully | Return `BLOCKED`; model text does not override process state. |
| Writer times out and the group is stopped | Preserve changes and recovery state; return recoverable `BLOCKED`. |
| Writer termination cannot be confirmed | Preserve the lock and quarantine the worktree. |
| Verification fails after retries | Preserve changes and recovery state; return `BLOCKED` with each executed result. |
| Coder changes branch or commit | Return `BLOCKED`; do not reset, stash, or rewrite its state. |
| Commit fails or leaves more changes | Return `BLOCKED` with the commit and final worktree state; preserve everything. |
| Successful invocation changed nothing | Return `DONE`, `changed:false`, empty `commit_id`, and a clean worktree. |
| Result JSON and process exit contradict each other | Controller treats the task as blocked and does not advance. |

## Testing

Automated tests use deterministic fake coders and real temporary Git worktrees.

### Lifecycle and ownership

- Start one coder that remains active, then attempt a second lifecycle in the same
  worktree. The second exits blocked and the first task's files are unchanged.
- Run lifecycles in two distinct worktrees. Their separate locks do not block each
  other.
- Finish a task normally and prove a later task can acquire ownership.
- Simulate an interrupted owner and prove fresh work is refused rather than silently
  deleting the ownership record.
- Produce a recoverable blocked state, then prove only the matching lifecycle resume
  can continue it.
- Prove a different task cannot start while partial work or recovery state remains.

### Hangs and process cleanup

- Have a fake coder spawn a child that keeps running after the parent emits a final
  answer. The lifecycle terminates the lingering group and returns blocked.
- Have a fake coder exceed its deadline after editing a file. The whole group stops,
  the file remains, the real session id is returned, and no later task is admitted.
- Have a verification command exceed its deadline and assert the same preservation
  and admission behavior.
- Simulate failure to confirm process termination and prove the worktree remains
  quarantined.

### Git and reporting

- Successful changes are committed by the lifecycle and leave a clean worktree.
- A no-change task creates no commit and reports an explicit no-change result.
- Primary checkouts and `main` or `master` branches are refused.
- An unexpected coder commit or branch switch blocks without rewriting history.
- Commit failure and commit-hook leftovers are reported without discarding files.
- `DONE` always carries the session id, changed paths, verification details, stopped
  writer state, and a commit id when changes exist.
- Malformed or contradictory JSON is rejected by controller-level contract tests.

### Verification commands

- One command remains supported.
- Multiple commands run in order in the coder worktree.
- The first failing command stops that pass; after a fix attempt the entire list runs
  again.
- Missing commands, mixed empty/non-empty commands, invalid shell syntax, and leading
  label tokens are rejected before coder invocation.
- A command timeout is distinct from a normal nonzero exit.
- Output is associated with the command that produced it.

### Workflow and documentation

- `/implement-plans` contains no implementation dispatch to `coder-delegator` and
  validates the direct lifecycle result before review or fast-forward.
- The coder agent file is absent and no documentation presents it as supported.
- README and the manual end-to-end smoke test describe synchronous task ownership,
  timeout recovery, concurrent-start refusal, and direct session resume.
- The complete existing test suite passes so reviewer delegation, pool selection,
  worktree isolation, and release tooling remain unchanged.

## Documentation changes

Update the README's command and workflow descriptions to say that `/implement-plans`
directly owns one coder task at a time. Explain the user-visible failure behavior:
timeouts stop the plan, partial work is preserved, and uncertain termination
quarantines the worktree.

Update the manual smoke runbook to cover:

1. a normal direct coder task;
2. a task resumed with its external coder session id;
3. an attempted concurrent invocation being refused;
4. a timeout preserving partial work;
5. recovery before a later task starts;
6. multiple verification commands and early rejection of a labeled command.

The changelog should describe the user consequence: delegated tasks now have
exclusive worktree ownership, so a late or hung worker cannot overlap the next task.

## Rollout

This change should land as one coherent safety update. The controller must not switch
to direct invocation before the lifecycle script owns committing and recovery, and the
script must not claim exclusive ownership before the controller stops dispatching the
old writer subagent.

No data migration is required. Existing clean worktrees continue normally. A dirty
worktree left by an earlier version is refused and requires manual inspection, which
matches the existing safety posture.

Release only after the deterministic concurrency and timeout tests, the full suite,
and the manual real-tool smoke checks pass.
