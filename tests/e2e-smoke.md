# E2E Smoke Tests (manual — real tools)

Prereqs: install and log in to whichever tools your pool entries use (`cursor-agent`,
`codex`, `claude`). `jq` and bash 5 on PATH. Plugin loaded (`CLAUDE_PLUGIN_ROOT`
set).

## Pool listing

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/pool.sh" list reviewers
bash "$CLAUDE_PLUGIN_ROOT/scripts/pool.sh" list coders
```

Expect: JSON with `role` and `entries` arrays matching
`.claude-plugin/reviewers.json` and `.claude-plugin/coders.json` (keys, labels,
harness names, default flags).

## Probe each pool entry

For every reviewer key in the shipped pool:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/probe.sh" --role reviewer --key <key>
```

Expect: `{"status":"READY",...}` for all four reviewers.

For every coder key:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/probe.sh" --role coder --key <key>
```

Expect: `{"status":"READY",...}` for both coders.

On `FAILED`, check `reason` (`auth`, `trust`, `not-installed`, `bad-model`,
`timeout`, `other`) and fix the environment before continuing.

---

## `/review` end to end

Run once per reviewer key against a real spec in your repo:

```
/review spec <path-to-a-real-spec>
```

For each run:

1. The command shows a menu built from `pool.sh list reviewers`; pick the entry
   under test.
2. Probe passes, then `reviewer-delegator` returns `REVIEWED` with a non-empty
   report and a `session_id`.
3. No files in the repo were modified by the review.
4. The report follows the rubric format (Summary / Strengths / Issues / Verdict).

Repeat with a plan review when you have a plan and its spec:

```
/review plan <plan-path> <spec-path>
```

## Re-review with session (Codex read-only)

Pick a Codex-backed reviewer (`codex-sol` in the shipped pool). Run a spec review,
note the `session_id`, then re-run with the same reviewer and pass that session:

```
/review spec <same-spec-path>
```

On re-review, do **not** show the menu again — resume the prior session. Confirm the
second call also returns `REVIEWED` with the same `session_id`. For Codex entries,
confirm the resumed run stays read-only (no sandbox escape; the harness passes
`-c sandbox_mode="read-only"` on resume).

---

## `/implement-plans` on a throwaway plan

On a feature branch with a clean working tree, create a tiny plan (one task, a simple
verify command) or use an existing small plan.

```
/implement-plans <plan-path>
```

Walk through and confirm:

1. **Worktree created** — `worktree.sh prepare` prints `READY` with a sibling
   `<branch>-work` path; the main checkout is untouched.
2. **Dependency folders cloned** — if the plan needs `node_modules` or similar, check
   `copied` in the prepare output; copies are isolated from the main tree.
3. **Task completed** — pick a coder from the menu; the probe passes; the direct
   lifecycle returns `DONE` with a real session id and commit id, and leaves the
   worktree clean.
4. **Review before advance** — confirm the controller reviews the reported commit
   before it fast-forwards the work branch onto the feature branch.
5. **Worktree removed** — `worktree.sh remove` succeeds when all work was merged.

If `remove` returns `REFUSED`, unmerged commits remain on the work branch — resolve
before deleting.

## Optional failure paths

- **Probe failure** — log out of one tool and confirm the command stops with clear
  advice matching the `reason` field.
- **BLOCKED review** — with auth broken, confirm no fabricated review is returned.
- **Dirty tree** — confirm `/implement-plans` refuses to start with uncommitted
  changes.

## Coder lifecycle safety checks

Run each check only in a throwaway repository or disposable branch.

1. Start a deliberately slow coding task. While it owns the worktree, start a second
   task against that worktree. Confirm the second task is refused before its coder
   starts and the first task's files remain unchanged.
2. Run a safe task that writes a marker file and then exceeds its timeout. Confirm
   the result is `BLOCKED`, the marker remains, and a fresh task cannot start.
3. Resume that task with both its reported lifecycle id and session id. Confirm the
   task can complete and commit. Confirm either identifier alone or a wrong identifier
   is refused.
4. Exercise quarantine with a controlled process that stays alive. Confirm recovery
   refuses while the recorded process is alive. Stop the process, run explicit
   recovery, and confirm the files are unchanged before resuming.
5. Give the lifecycle two verification commands that append different markers to a
   file. Confirm they run in order. Make the second fail once and confirm both commands
   rerun in the same order.
6. Pass a verification value beginning with `server:`. Confirm input validation
   rejects it before a coder starts.

Clean up only after every spawned process is confirmed stopped and each disposable
worktree is clean.

## Cleanup

Remove any throwaway branches, worktrees, and temp plans you created for these checks.
