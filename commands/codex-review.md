---
description: Get an independent GPT-5.6 Sol review of a design spec or implementation plan, removing the bias of self-review. Usage: /codex-review <spec|plan> <doc-path> [spec-path]
argument-hint: <spec|plan> <doc-path> [spec-path]
---

You are the **controller**. You will obtain an INDEPENDENT review of the document
named in `$ARGUMENTS` by delegating to <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label --> through the
`codex-reviewer-delegator` subagent. You do NOT review it yourself — that is the
point: the model that authored the document must not be the one that grades it.

A second independent reviewer, <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->, is also
available via `/cursor-review`. Use whichever the user asked for; if they didn't say,
ask.

## Parse arguments
`$ARGUMENTS` is `<target> <doc-path> [spec-path]` where `target` is `spec` or `plan`.
- If `target` or `doc-path` is missing, ask the user for them and stop.
- `spec-path` is optional and used only for `plan` reviews (the spec the plan must satisfy).

## Preflight (do this first, stop on failure)

1. **Doc exists?** Confirm `doc-path` is a readable file. If not, tell the user and stop.
2. **codex healthy? Probe for real** — do not trust cached login state alone. Run an
   actual read-only headless probe:
   ```
   CODEX_REVIEWER_MODEL=$(jq -er '.codex_reviewer.id // empty' "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/models.json")
   if [[ -z "$CODEX_REVIEWER_MODEL" ]]; then
     echo "error: could not read .codex_reviewer.id from models.json"; exit 2
   fi
   codex exec --json -s read-only -m "$CODEX_REVIEWER_MODEL" "Reply with the single word READY." < /dev/null
   ```
   If the run exits non-zero, emits a `turn.failed` event, or the final `agent_message`
   text is not `READY` (auth error, timeout, or anything else), tell the user to run
   `codex login` (suggest they type `! codex login`) and STOP.

## Determine lenses (spec reviews only)

For `target spec`, decide which review lenses apply by reading the spec:
- **backend** — always include.
- **frontend** — include if the spec describes UI components, client state, routes,
  screens, or styling.
- **ui** — include if the spec describes user-facing flows, layouts, or UX.

If it is genuinely unclear whether the spec has a frontend/UI surface, ask the user
once. For `target plan`, do not pass lenses.

## Dispatch the reviewer

Dispatch the **`codex-reviewer-delegator`** subagent (NOT a general-purpose
subagent). Give it:
- the `target` (`spec` or `plan`),
- the `doc-path`,
- the `spec-path` if this is a plan review,
- the comma-separated `lenses` if this is a spec review.

It shells to the read-only reviewer and returns the report verbatim, plus a
`session_id` and a `status` (REVIEWED | BLOCKED).

If it returns **BLOCKED**, surface the diagnostic to the user and stop — do not
fabricate a review or substitute your own. Fixing the environment (login) is the
user's job.

## Act on the review (you, Opus)

Apply the **superpowers:receiving-code-review** skill to the returned report. Engage
each finding with technical rigor:
- Verify it against the document and the actual codebase before accepting it.
- Where the reviewer is right, plan or make the fix.
- Where the reviewer is wrong, push back with specific reasoning — do not perform
  agreement, and do not reflexively dismiss.

Present a triaged summary to the user (accepted / rejected-with-reason / needs-their-
decision). Offer a fix → re-review loop; on re-review, pass the prior `session_id` to
the subagent so the reviewer resumes its own context.

## Final step: Review Effectiveness Summary (ALWAYS do this)

Produce a short **Review Effectiveness Summary** (≈10–15 lines). Do BOTH: print it,
and append it as a dated entry to `docs/cursor-reviewer/effectiveness-log.md` in the
working repo (create the dir/file if missing; if not writable or the user objects,
just print it and say where it would have gone). Capture:

- **Reviewer:** Codex/GPT-5.6 Sol.
- **Run:** date · target · doc path · lenses used.
- **Findings:** count by severity (Critical/Important/Minor) · the reviewer's verdict.
- **Triage outcome:** how many findings you accepted vs pushed back on, and why.
- **Reviewer quality:** were findings specific and codebase-grounded, or vague? Any
  false positives (flagged a non-issue) or things it missed that you caught?
- **Environment friction:** auth/timeout, BLOCKED, missing `session_id`.
- **Recommendations:** concrete changes to the rubrics or dispatch prompt that would
  improve the next review.

Base every line on what actually happened this run — do not invent metrics.
