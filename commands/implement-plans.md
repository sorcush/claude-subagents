---
description: Implement a written plan by delegating each task to a coder you pick out of the configured pool, working in an isolated git worktree, while you review. Usage: /implement-plans <plan-path>
argument-hint: <path-to-plan-file>
---

You are the **controller**. You will implement the plan at `$ARGUMENTS` by delegating
each task's implementation to a coder from the plugin's configured pool (through the
`coder-delegator` subagent) while YOU do the planning extraction and all review. The
coder writes code; you never let it review.

You do not know or need to know which models are in the pool. You read the pool at run
time and let the user choose.

## Preflight (do this first, stop on failure)

1. **Plan path given?** If `$ARGUMENTS` is empty, ask the user for the plan file path
   and stop until provided.

2. **Branch safety.** Run `git rev-parse --abbrev-ref HEAD`. If it is `main` or
   `master`, tell the user and ask them to create a feature branch before you start.
   Do not implement on main or master.

3. **Clean working folder.** Run `git status --porcelain`. If there is any staged or
   unstaged change, stop and ask the user to commit or stash it. The worktree is
   created from the last commit, so uncommitted work would be invisible to the coder
   and would still be sitting in the way when the fast-forward happens later.

4. **Create the worktree.**
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh" prepare
   ```
   It prints `{"status":"READY","worktree":...,"work_branch":...,"feature_branch":...,"copied":[...],"skipped":[...]}`.
   Keep the `worktree` path — every coder dispatch needs it. If the command fails,
   show its error and stop; it refuses rather than guessing whenever the state is
   ambiguous. If `skipped` is not empty, warn the user that those dependency folders
   were not brought across, so a verify command may fail for a missing dependency.

5. **Load context.** Read the plan file at `$ARGUMENTS`. If it references a spec, read
   that too — you'll need it for spec-compliance review.

## Then: run subagent-driven development with the implementer overridden

Invoke the **superpowers:subagent-driven-development** skill and follow it, with these
FOUR overrides (state them to yourself before starting):

1. **Implementer = coder-delegator, working in the worktree.** For each task's
   implementation step:
   1. **Ask which coder** (pool.sh list coders), unless the user already fixed one for this run:
      ```
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/pool.sh" list coders
      ```
      Build the menu from those entries, in the order given — never reorder them.
      Four entries or fewer: use the pop-up menu, showing each `label`, marking the
      `default` entry as recommended in place. More than four: print a numbered list.

      The **first** time you ask in a run, ask two things together: which coder, and
      whether to use it for the rest of the run. If the user says yes, do not show the
      menu again for the coder role.

      Never write a model name into this file. The menu comes from the script.
   2. **Probe it:**
      ```
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/probe.sh" --role coder --key "<chosen key>"
      ```
      On `FAILED`, give advice matching the `reason` field (`auth` → log in,
      `trust` → trust the workspace, `not-installed` → install the tool,
      `bad-model` → fix `.claude-plugin/coders.json`, otherwise show the
      `diagnostic`) and STOP. Do not say "log in" unless `reason` is `auth`.
   3. **Dispatch `coder-delegator`** (NOT a general-purpose subagent). Give it: the
      coder key, the **worktree path**, the task's full text, scene-setting context,
      and a concrete **verify command** for that task (derive it from the plan's
      verification steps; if a task has none, use the plan's general test command, or
      ask the user once).

2. **No interactive implementer Q&A.** The coder is one-shot; `coder-delegator` cannot
   hold a back-and-forth. If it returns **NEEDS_CONTEXT** or **BLOCKED**, treat it
   exactly as the skill says: provide more context and re-dispatch, break the task
   smaller, switch the verify command, or escalate to the user. Never silently retry
   unchanged.

3. **Reviews stay with you.** Keep the skill's two-stage review after each task — spec
   compliance first, then code quality — reading the change **in the worktree**. The
   coder is NEVER a reviewer, and reviewers from the pool are not used for task
   reviews.

   When a task needs changes, send your comments back to the **same coder session** by
   re-dispatching with the `session_id` the subagent returned. A fix is a follow-up,
   not a fresh delegation, so do NOT show the coder menu again.

4. **Fast-forward after each passing task.** Once your review passes, in the main
   folder run:
   ```
   git merge --ff-only <work-branch>
   ```
   This is what moves reviewed work onto the feature branch. If it fails, the feature
   branch has commits of its own and is no longer an ancestor of the work branch —
   most likely someone committed during the run. STOP and tell the user. Do not
   attempt a real merge; a conflict mid-run is worse than stopping. If the task
   changed nothing, this is a no-op and not an error.

Everything else about subagent-driven-development (serial dispatch, TodoWrite task
tracking, fix-and-re-review loops, handling DONE / DONE_WITH_CONCERNS) is UNCHANGED.

## When all tasks are done

1. **Tear down the worktree:**
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh" remove
   ```
   If it returns `REFUSED`, it found commits on the work branch that never reached the
   feature branch. Nothing was deleted. Show the `unmerged` list to the user and
   resolve it before continuing.

2. **Finish the branch.** Follow **superpowers:finishing-a-development-branch**. That
   skill verifies the tests pass, works out the base branch itself, and offers merge,
   push and open a pull request, keep the branch as-is, or discard. Do not reimplement
   any of that here.

## Final step: Delegation Effectiveness Summary (ALWAYS do this)

After `finishing-a-development-branch` completes — or if the run is abandoned partway —
produce a short **Delegation Effectiveness Summary**. This is the raw material for
improving the plugin, so it is required even when the run went well.

Do BOTH:
1. Print the summary to the user.
2. Append it as a dated entry to `docs/cursor-coder/effectiveness-log.md` in the
   working repo (create the dir and file if missing). If that repo is not writable or
   the user objects, just print it and say where it would have gone.

Keep it tight (about 10 to 15 lines). Capture:

- **Run:** date, plan path, branch, number of tasks delegated.
- **Coders used:** the `label` and key used for each task. If the user switched
  mid-run, say where and why.
- **Outcome:** how many tasks passed verification first try (`attempts:0`) versus
  needed fix loops, total fix attempts across the run, any `BLOCKED` or
  `NEEDS_CONTEXT` and the cause.
- **Coder fidelity:** did it follow the task specs? Note any out-of-scope file edits
  or autonomous commits you had to reconcile, and whether they were correct.
- **Worktree behaviour:** anything `prepare` skipped, verify failures caused by a
  missing dependency rather than by the code, and whether `remove` refused.
- **Environment friction:** probe failures, timeouts, missing `session_id` or resume
  failures, latency or attempt-count outliers.
- **Reliability flags (most important):** any case where `coder-delegator` did NOT
  actually delegate — it wrote code itself, reported `DONE` without a real
  `session_id`, committed outside the worktree, or otherwise bypassed the script.
  Call these out explicitly; they mean the delegation contract was broken.
- **Recommendations:** concrete changes to the plan format, dispatch prompts, the
  subagents, or the scripts that would make delegation smoother next time.

Base every line on what actually happened this run — do not invent metrics.
