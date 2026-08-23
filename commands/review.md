---
description: Get an independent review of a design spec or implementation plan from a reviewer you pick out of the configured pool, removing the bias of self-review. Usage: /review <spec|plan> <doc-path> [spec-path]
argument-hint: <spec|plan> <doc-path> [spec-path]
---

You are the **controller**. You will obtain an INDEPENDENT review of the document
named in `$ARGUMENTS` by delegating to a reviewer from the plugin's configured pool,
through the `reviewer-delegator` subagent. You do NOT review it yourself — that is the
point: the model that authored the document must not be the one that grades it.

You do not know or need to know which models are in the pool. You read the pool at
run time and let the user choose.

## Parse arguments
`$ARGUMENTS` is `<target> <doc-path> [spec-path]` where `target` is `spec` or `plan`.
- If `target` or `doc-path` is missing, ask the user for them and stop.
- `spec-path` is optional and used only for `plan` reviews (the spec the plan must satisfy).

## Preflight (do this first, stop on failure)

1. **Doc exists?** Confirm `doc-path` is a readable file. If not, tell the user and stop.

2. **Ask which reviewer.** Run pool.sh list reviewers:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/pool.sh" list reviewers
   ```
   That prints `{"role":"reviewers","entries":[{"key","label","harness","default"}]}`.
   Build the menu from those entries, in the order given — never reorder them.
   - Four entries or fewer: use the pop-up menu, one option per entry, showing its
     `label`. Mark the entry whose `default` is true as recommended, in place.
   - More than four: print a numbered list and ask the user to type a number.

   Never write a model name into this file. The menu comes from the script.

3. **Probe the chosen reviewer for real.** A cached login is not proof:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/probe.sh" --role reviewer --key "<chosen key>"
   ```
   It prints `{"status":"READY"|"FAILED","reason":...,"diagnostic":...}`. On `FAILED`,
   give advice that matches `reason` and STOP:
   - `auth` — the tool is not logged in. Tell the user which tool and suggest they
     type `! <tool> login`.
   - `trust` — the workspace is not trusted by that tool.
   - `not-installed` — the tool is not on `PATH`.
   - `bad-model` — the model id in the pool file is wrong; point at
     `.claude-plugin/reviewers.json`.
   - `timeout` or `other` — show the `diagnostic` verbatim.

   Do not tell the user to log in unless `reason` is `auth`.

## Determine lenses (spec reviews only)

For `target spec`, decide which review lenses apply by reading the spec:
- **backend** — always include.
- **frontend** — include if the spec describes UI components, client state, routes,
  screens, or styling.
- **ui** — include if the spec describes user-facing flows, layouts, or UX.

If it is genuinely unclear whether the spec has a frontend or UI surface, ask the user
once. For `target plan`, do not pass lenses.

## Dispatch the reviewer

Dispatch the **`reviewer-delegator`** subagent (NOT a general-purpose subagent). Give it:
- the **reviewer key** the user chose,
- the `target` (`spec` or `plan`),
- the `doc-path`,
- the `spec-path` if this is a plan review,
- the comma-separated `lenses` if this is a spec review.

It shells to the read-only reviewer and returns the report verbatim, plus a
`session_id` and a `status` (REVIEWED | BLOCKED).

If it returns **BLOCKED**, surface the diagnostic to the user and stop — do not
fabricate a review or substitute your own.

## Act on the review (you, the controller)

Apply the **superpowers:receiving-code-review** skill to the returned report. Engage
each finding with technical rigor:
- Verify it against the document and the actual codebase before accepting it.
- Where the reviewer is right, plan or make the fix.
- Where the reviewer is wrong, push back with specific reasoning — do not perform
  agreement, and do not reflexively dismiss.

Present a triaged summary to the user (accepted / rejected-with-reason / needs-their-
decision). Offer a fix and re-review loop.

**On re-review, pass the prior `session_id`** so the reviewer resumes its own context
and can grade whether its earlier findings were actually addressed. A resumed review
is a follow-up, not a fresh delegation, so do NOT show the menu again — keep the same
reviewer.

## Final step: Review Effectiveness Summary (ALWAYS do this)

Produce a short **Review Effectiveness Summary** (about 10 to 15 lines). Do BOTH:
print it, and append it as a dated entry to `docs/cursor-reviewer/effectiveness-log.md`
in the working repo (create the dir and file if missing; if not writable or the user
objects, just print it and say where it would have gone). Capture:

- **Reviewer:** the `label` of the entry the user chose, and its key.
- **Run:** date, target, doc path, lenses used.
- **Findings:** count by severity (Critical / Important / Minor) and the verdict.
- **Triage outcome:** how many findings you accepted versus pushed back on, and why.
- **Reviewer quality:** were findings specific and codebase-grounded, or vague? Any
  false positives, or things it missed that you caught?
- **Environment friction:** probe failures, timeouts, BLOCKED, missing `session_id`.
- **Recommendations:** concrete changes to the rubrics or dispatch prompt that would
  improve the next review.

Base every line on what actually happened this run — do not invent metrics.
