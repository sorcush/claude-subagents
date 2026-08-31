---
name: cursor-coder
description: Use when implementing a bounded task or written plan with Cursor.
version: 1.0.0
metadata:
  hermes:
    tags: [cursor, coding, delegation, worktree]
    requires_toolsets: [terminal, file, todo]
---

# Cursor Coder

<!-- ADAPTER-PARITY: commands/implement-plans.md; policy: docs/adapter-parity.md -->

Before dispatch, load and confirm `subagent-driven-development`,
`receiving-code-review`, and `finishing-a-development-branch` are available.
Stop if a required workflow cannot be loaded.

Use ad-hoc mode for one bounded task and one explicit non-empty verification
command. For a written plan, read the complete plan and specification, preserve
the Superpowers task ledger and five-round review limit, execute tasks serially,
and replace only the implementer boundary with this adapter.

Generate one cryptographically random 16-lowercase-hex run ID. Require a clean
non-main feature branch. Prepare one run-scoped worktree, then probe:

```bash
"${HERMES_SKILL_DIR}/../../scripts/dispatch.py" worktree --action prepare \
  --repo "$REPOSITORY" --run-id "$RUN_ID"
"${HERMES_SKILL_DIR}/../../scripts/dispatch.py" probe \
  --role coder --run-id "$RUN_ID"
```

Dispatch with the latest generation, exact worktree, task file, and verification
command. Use `--max-retries 3` initially. Require `DONE`, `verified:true`, a
non-empty session ID, and an empty diagnostic. Inspect the actual staged diff
and independently rerun verification in the worktree.
The adapter durably enforces no more than nine Cursor calls, five controller
review rounds, and five active worker hours for each plan task. Never start a
fresh run to bypass a task's limits.

The adapter stages the verified worker tree but Cursor must never commit. Create
the controller commit in the worktree, then call `state --action
record-reviewing`. Perform specification and code-quality review yourself.
For corrections, dispatch from `reviewing` with the same run ID, latest
generation, exact prior `--session`, and `--max-retries 0`; never let the coder
approve its own work.
If a blocked run reports a protected-state mismatch or has no safe resumable
identity, preserve the worktree and state record and stop instead of recapturing
the current Git state as a clean baseline.

Before integration call `state --action record-integration --commit <sha>`.
Fast-forward only with `git merge --ff-only <sha>`, then call `state --action
show` to reconcile to `integrated`. Never push, publish, remotely merge, rewrite
history, alter Git configuration, or use force operations.
For another serial plan task, keep the run ID but dispatch from `integrated`
without `--session` and with the initial `--max-retries 3`. The adapter resets
the completed task's session, budgets, protected identity, and verified baseline;
require a new non-empty session ID. Same-session `--max-retries 0` applies only
to review corrections before that task is integrated.

After the final reviewed commit is integrated, remove with the latest generation:

```bash
"${HERMES_SKILL_DIR}/../../scripts/dispatch.py" worktree --action remove \
  --repo "$REPOSITORY" --run-id "$RUN_ID" \
  --expected-generation "$GENERATION"
```

On failure preserve the worktree and report feature branch, work branch,
worktree, run ID, generation, last session ID, last integrated commit,
unintegrated commits, and exact inspect/resume/remove commands. Keep already
integrated plan-task commits. Append a `host: hermes` effectiveness entry only
after the final safety checks.
