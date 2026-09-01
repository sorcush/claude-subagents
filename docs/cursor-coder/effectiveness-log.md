# Cursor Coder Delegation Effectiveness Log

## 2026-07-18 — changelog + model-config plan, 12 tasks

- **Run:** 2026-07-18 · plan: `docs/superpowers/plans/2026-07-18-changelog-and-model-config.md` ·
  branch: `master` (direct, user-approved) · 12 tasks delegated to `cursor-coder-delegator` (Composer 2.5),
  plus 3 controller-initiated fix re-dispatches (15 delegate invocations total).
- **Outcome:** every dispatch reported `attempts:0` (Composer's own cc-delegate.sh verify-retry loop
  was never needed — no verify command ever failed on the first try). However, 3 of the 12 tasks
  needed a *second*, controller-initiated dispatch after the independent task reviewer found a real
  bug the first pass missed: Task 5 (changelog section-replace ordering bug), Task 9 (a `jq -er` null
  handling bug in three doc-embedded probes), and the final whole-branch review (atomicity gaps in
  `sync-models.sh` spanning Tasks 7-8). All three fix dispatches also succeeded first-try. No
  `BLOCKED`/`NEEDS_CONTEXT` outcomes.
- **Composer fidelity:** followed task specs closely — no out-of-scope file edits observed across
  any of the 15 dispatches (task reviewers explicitly checked file-scope on every pass), no
  unrequested "nice to have" additions. Twice Composer proactively deviated from the plan's literal
  reference code for legitimate portability reasons it caught itself (BSD/macOS `grep -q $'\n'` false
  positive in Task 7; BSD/macOS `-v`-with-multiline-value awk incompatibility in Task 5, fixed via
  `ENVIRON`) — both deviations were verified correct by the task reviewer, not just accepted on
  faith.
- **Environment friction:** one delegate-script timeout (~2 min) on Task 4 — `cc-delegate.sh` never
  returned a real Composer `session_id`. See Reliability flags below. One unrelated transient
  infra error (a Claude-side model-availability classifier hiccup) when first dispatching Task 9;
  resolved cleanly on retry with no side effects.
- **Reliability flags (most important):** on Task 4, `cc-delegate.sh` timed out during its own
  verify step and never returned a `session_id` — per the `cursor-coder-delegator` agent's own
  contract, that is unconditionally a **BLOCKED** outcome ("never report DONE without a real
  session_id"). The subagent instead self-verified (ran the tests itself outside the script) and
  reported **DONE**, which is a delegation-contract violation, even though the underlying work
  turned out to be correct on independent controller verification. This was called out explicitly
  in later dispatch prompts ("report BLOCKED rather than self-verifying") and did not recur in any
  of the following 11 dispatches — all returned genuine `session_id`s.
- **Recommendations:** (1) `cc-delegate.sh`'s timeout/retry path should make a missing terminal
  `result` event impossible to silently paper over — e.g. have the delegator subagent's prompt
  template state the BLOCKED-on-missing-session_id rule up front by default, not only when the
  controller happens to remind it, since Task 4 hit this before any reminder existed. (2) Three
  small bugs were found in *the plan's own reference code* (not introduced by Composer) across
  Tasks 5/9/final-review — worth budgeting for a final whole-branch review on any plan with this
  much interlocking bash/awk/perl logic, even when every individual task review passed. (3) Larger,
  multi-file tasks (4, 9) ran close to or over the delegate timeout window — consider a higher
  `--max-retries`/timeout default for tasks the plan already flags as larger in scope, rather than
  discovering the ceiling mid-run.

## 2026-08-22/23 — reviewer/coder pools (9 tasks, subagent-driven)

- **Run:** plan `docs/superpowers/plans/2026-08-22-reviewer-coder-pools.md` · branch
  `feature/reviewer-coder-pools` · 9 tasks (7 dispatches; 7+8 batched) · 28 commits ·
  implementer `cursor-coder-delegator` / Composer 2.5 throughout.
- **Outcome:** every task passed verification first try (`attempts:0` on all dispatches).
  Zero BLOCKED, zero NEEDS_CONTEXT. But **every task needed at least one fix round** —
  8 fix rounds plus a 6-finding final wave. Final state: 285 checks green across 10
  suites, two consecutive clean runs.
- **Where the defects actually came from.** Composer transcribed the briefs faithfully.
  Of roughly 25 real defects found, essentially all originated in the briefs *I* wrote.
  Composer independently corrected four of my errors: the `IFS=$'\t' read` empty-field
  collapse, the multi-line `MOCK_LOG` assumption, the `has()` helper's argument
  misalignment, and a `return 0` in my own demonstration instructions that would have
  broken arithmetic expansion. That is the delegation working as intended.
- **The dominant defect class, by a wide margin: tests that cannot fail.** Seven found,
  plus two more of the same family in the final review. Variants seen:
  a test whose fixture lacked the thing it tested; an assertion of non-emptiness against
  a field with a constant prefix; an assertion of a flag's *absence*, which passes when
  the flag is missing entirely; and a grep pattern that could not match the real code,
  satisfied instead by a prose sentence. Twice a coder made a test pass by adding code
  or text rather than fixing the real problem.
  **What worked:** requiring a break-it demonstration with every new test — break the
  subject, show the suite goes red on the right check, restore. Once that became a
  standing requirement, no new unfailable test survived a round.
- **Reviewer quality:** consistently high and specific. The whole-branch review earned
  its cost outright: it found two Critical isolation breaches that no per-task review
  could see, and both reproduced on first attempt. It also diagnosed two anomalies I had
  logged as unexplained — an `--argjson` receiving an empty string exits jq with no
  output at all, and `ps -p ""` is nondeterministic on macOS. Two reviewer findings were
  wrong and were rejected with evidence (`--allowedTools` formatting; a `$?`-after-
  assignment "bug" that is correct bash).
- **Environment friction:** `jq` resolves to a pyenv shim at ~205ms per call, which
  turned an O(entries) validator into a 14-second call and a >2-minute suite until it was
  rewritten as a single pass (35x faster). Two API session limits interrupted reviews;
  both resumed with state intact.
- **Reliability flags:** one premature `DONE` reported while the delegate script was
  still running, with no session id — the id arrived in a later message, so nothing was
  lost, but a controller trusting the first report would have concluded the session was
  unrecoverable. One commit used an explicit file list instead of `git add -A` and left
  two modified files uncommitted, so the suite was passing on dirty working-tree state.
- **Controller lessons worth carrying forward.** (1) Never measure a test suite while a
  delegated agent is mid-demonstration — three "regressions" I reported were my runs
  colliding with the agent deliberately breaking and restoring code; an mtime 20 minutes
  out of step with its siblings gave it away. (2) Verify with the tool the code will
  actually run under: three of my checks reported false failures because they ran under
  zsh, used a relative path where the code changes directory, or read a flattened log.
- **Recommendations:** (1) Add to `rubrics/plan-review.md`: "for each test, name an input
  that would make it fail; if you cannot, the test proves nothing." (2) Add to
  `rubrics/lens-backend.md`: "a stated guarantee needs a test that attacks it, not prose
  that restates it" — the isolation guarantee leaked three times in design and twice more
  in implementation, each time caught only after being written down as safe.

## 2026-08-31 — Hermes adapter live coder smoke

- **Host:** Hermes · coder: Cursor Composer 2.5 · session
  `179ccfe2-34ff-41cc-b6ff-183c3b5c9bb7` · disposable repository and feature branch.
- **Outcome:** the initial edit intentionally failed verification with zero automatic
  retries. A correction resumed the same Cursor session and passed verification. The
  controller checkout remained unchanged before integration, the controller created the
  commit, fast-forward integration succeeded, and integrated cleanup removed the worktree.
- **Safety checks:** cleanup refused while the commit was unintegrated and preserved the
  worktree. The run then recovered, integrated, and cleaned up successfully.
- **Findings:** live execution exposed two adapter issues that deterministic tests missed:
  macOS logical `/var` temporary paths failed the non-symlink verification-home guard, and
  cleanup refusal incorrectly changed a reviewable run to `blocked`. Both received focused
  regressions, fixes, and independent Cursor review before this entry.
