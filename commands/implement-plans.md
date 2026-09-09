---
description: Implement a written plan by delegating each task to a coder you pick out of the configured pool, working in an isolated git worktree, while you review. Usage: /implement-plans <plan-path>
argument-hint: <path-to-plan-file>
---

You are the **controller**. Implement the plan at `$ARGUMENTS` one task at a time.
An external coder edits the isolated worktree. You extract tasks, validate results,
review every committed change, and advance the feature branch.

Read the coder pool at run time and let the user choose. Never hardcode a model name.

## Preflight

1. If `$ARGUMENTS` is empty, ask for the plan path and stop.
2. Run `git rev-parse --abbrev-ref HEAD`. On `main` or `master`, ask the user to
   create a feature branch and stop.
3. Run `git status --porcelain`. If it is not empty, ask the user to commit or stash
   the changes and stop.
4. Prepare the isolated worktree:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh" prepare
   ```

   Keep the returned `worktree`, `work_branch`, and `feature_branch`. Stop on a
   refusal. If `skipped` is not empty, warn that verification may lack dependencies.
5. Read the plan and any referenced specification.

## Run each task synchronously

Use the task-by-task and two-stage review structure from
**superpowers:subagent-driven-development**, with the implementation stage replaced
by the direct lifecycle below. Do not create an implementation subagent. Do not use
agent-list or agent-stop operations for the coder path.

For each task:

1. Record the worktree's current commit as the task base.
2. Ask which coder to use unless the user already selected one for the run:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/pool.sh" list coders
   ```

   Preserve the returned order. With four entries or fewer, show a menu and mark the
   configured default as recommended. With more than four, show a numbered list. On
   the first choice, also ask whether to reuse that coder for the remaining tasks.
3. Probe the selected coder:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/probe.sh" --role coder --key "<chosen key>"
   ```

   On failure, explain the returned reason and stop. Recommend login only for `auth`.
4. Write the task text and relevant context to a temporary task file. Convert every
   verification step into a separate shell-safe `--verify-cmd` argument. Do not put
   labels such as `server:` or `client:` inside a command. Use one empty
   `--verify-cmd` only when the plan explicitly has no automated verification.
5. Derive a concise, non-empty, single-line commit message from the task title.
6. Invoke the lifecycle directly and wait for it to exit:

   ```bash
   result_file=$(mktemp)
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/code-delegate.sh" \
     --coder "$coder_key" \
     --task-file "$task_file" \
     --cwd "$worktree" \
     --commit-message "$commit_message" \
     --verify-cmd "$verify_one" \
     --verify-cmd "$verify_two" >"$result_file"
   delegate_rc=$?
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/validate-code-result.sh" \
     --exit-code "$delegate_rc" --result-file "$result_file"
   validator_rc=$?
   rm -f "$result_file"
   ```

   Omit extra verification arguments when they do not exist. Add `--session` for a
   review-fix follow-up. Add both `--session` and `--lifecycle-id` only when resuming
   that same incomplete task.
7. If validation exits 2, stop: the result is malformed or contradictory. If the
   lifecycle returns `BLOCKED`, show its `lifecycle_id`, `session_id`,
   `files_changed`, and `diagnostic`, then stop before starting another task.
   - When `writer_stopped` is true and the result is recoverable, resume only this
     task with its matching lifecycle and session identifiers.
   - When writer shutdown is uncertain or the worktree remains quarantined, do not
     delete ownership markers. After the user confirms no writer remains, run:

     ```bash
     bash "${CLAUDE_PLUGIN_ROOT}/scripts/code-delegate.sh" recover \
       --cwd "$worktree" --lifecycle-id "$lifecycle_id"
     ```

     If the coder changed the branch or created a commit, the user must first inspect
     and choose how to restore the expected branch state. Recovery never makes that
     decision.
8. Review only a valid `DONE` result. Before review, require a non-empty `session_id`,
   `writer_stopped:true`, `worktree_clean:true`, and either successful verification
   or explicit no-verification mode. Require `changed:true` to have a non-empty
   `commit_id`; require `changed:false` to have an empty `commit_id`.

The coder's answer text and the host process status are context only. Neither proves
completion. The validator's observed lifecycle fields are the completion evidence.

## Review and advance

Keep the two review stages after each task: specification compliance first, then code
quality. Read the committed range from the recorded task base through the reported
`commit_id`. The external coder is never a reviewer.

When review finds a problem, write the feedback to a new task file and call the same
lifecycle directly with the same coder `session_id`. Validate the result again and
repeat both review stages. Do not show the coder menu for a same-session fix.

After approval, fast-forward from the main folder:

```bash
git merge --ff-only <work-branch>
```

Stop if fast-forward fails. Do not create a merge commit. Before starting the next
task, confirm the coder result is valid, the worktree is clean, and its private Git
directory has no lifecycle ownership or recovery state.

## Finish

When all tasks pass review:

1. Remove the worktree:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh" remove
   ```

   If removal is refused, show the unmerged commits and preserve the worktree.
2. Follow **superpowers:finishing-a-development-branch**. Do not merge, push, open a
   pull request, keep, or discard the branch without the user's choice.

## Delegation Effectiveness Summary

Always print a concise 10–15 line summary and append it as a dated entry to
`docs/cursor-coder/effectiveness-log.md` in the working repository when writable.
Include:

- date, plan, branch, and delegated task count;
- coder label and key for each task;
- first-pass successes, fix attempts, blocks, and their causes;
- spec fidelity, out-of-scope edits, or unauthorized Git changes;
- copied or skipped dependencies and worktree-removal outcome;
- probe failures, timeouts, missing sessions, resume failures, and unusual latency;
- any case where the direct lifecycle did not invoke the selected external coder,
  reported success without a real session, committed outside the worktree, or
  otherwise broke the delegation contract;
- concrete improvements for the plan, prompts, or lifecycle scripts.

Use only facts observed during the run.
