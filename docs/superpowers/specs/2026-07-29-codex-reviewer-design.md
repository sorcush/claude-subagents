# codex-reviewer — Design Spec

**Date:** 2026-07-29
**Status:** Approved for planning

## Goal

Add a **third, independent reviewer** to this plugin: design-spec and implementation-plan
review delegated to **GPT-5.6 Sol** (`gpt-5.6-sol`) running headless inside OpenAI's
**Codex CLI** (`codex exec`), alongside the existing Cursor-backed reviewer (Grok 4.5 via
`cursor-agent`). This is a parallel option, not a replacement — the controller offers a
choice of reviewer rather than defaulting to one.

## Problem

`cursor-reviewer-delegator` / `/cursor-review` already solves the self-review conflict of
interest (the model that authored a spec/plan should not be the one that grades it) by
delegating to Grok 4.5 via `cursor-agent`. A second, independently-sourced reviewer (a
different vendor, different model, different CLI) gives a genuine second opinion and lets
the user pick per review — useful when they want more than one independent read, or when
`cursor-agent` isn't available/healthy.

Code review of implemented changes remains out of scope, as with the existing reviewer.

## Scope

**In scope:**
- Independent review of a **design spec** (`target: spec`) via Codex/GPT-5.6 Sol.
- Independent review of an **implementation plan** (`target: plan`) via Codex/GPT-5.6 Sol.
- The same **lens** model as the existing reviewer (backend always; frontend/ui layered on
  for spec reviews) — reusing the existing rubric files verbatim.
- Grounding the review in the **existing codebase**, enabled by Codex's read-only sandbox
  (`-s read-only`), which permits reading/exploring the repo but not writing to it.
- A workflow convention: at the superpowers self-review gates (brainstorming step 7,
  writing-plans Self-Review), the controller **asks the user which reviewer to use**
  (Cursor/Grok or Codex/GPT-5.6 Sol) before dispatching either one. An explicit
  `/cursor-review` or `/codex-review` invocation is already the user's choice and needs no
  extra prompt.

**Out of scope (this change):**
- Code review of implemented changes (same as the existing reviewer).
- Visual/rendered UI review — Codex, like `cursor-agent`, cannot render or screenshot.
- A combined "run both reviewers at once" command. Each reviewer is dispatched
  independently; running both is a manual choice (invoke one command, then the other),
  not automated here.
- Renaming the plugin/repo. `cursor-subagent-cc` keeps its name; only its description and
  keywords are updated to mention Codex.
- Changing the existing Cursor/Grok reviewer's behavior, model, or `models.json` key.

## Roles

| Role | Who | Responsibility |
|------|-----|----------------|
| Controller | Opus session | Authors spec/plan, asks the user which reviewer to use at self-review gates, dispatches the chosen review command, critically triages findings via `superpowers:receiving-code-review`, decides fixes. |
| Delegator | `codex-reviewer-delegator` (Haiku subagent, `tools: Bash, Read`) | Runs `cx-delegate.sh`, relays the report verbatim. Never edits, judges, filters, or fabricates. |
| Reviewer | `gpt-5.6-sol` via `codex exec -s read-only` (read-only) | Reads the document **and** explores the existing repo, produces the structured review. |

## Technical grounding (verified against the installed CLI)

- `codex-cli 0.144.1`, logged in via ChatGPT (`codex login status` → "Logged in using ChatGPT").
- Model id **`gpt-5.6-sol`** confirmed working: `codex exec --json -s read-only -m gpt-5.6-sol
  "Reply with the single word READY." < /dev/null` returns the model's answer and exits 0.
- Read-only enforcement: **`-s read-only`** (sandbox policy flag on `codex exec`). Combined
  with the delegator having no `Write`/`Edit` tools, the reviewer cannot mutate the repo
  (defense in depth, same posture as the Cursor reviewer's `--mode ask`).
- `codex exec` has **no interactive approval flag** (`-a/--ask-for-approval` exists on the
  top-level `codex` command but not on `exec`); headless runs simply execute within the
  sandbox and return failures to the model. No hang risk observed.
- **Redirect stdin from `/dev/null`**: without it, `codex exec` prints "Reading additional
  input from stdin..." and waits for stdin (a documented pipe-append behavior); `< /dev/null`
  makes it a no-op immediately.
- **Output/events (`--json`)**: newline-delimited JSON.
  - `{"type":"thread.started","thread_id":"<uuid>"}` — the session id for resuming.
  - `{"type":"item.completed","item":{"id":...,"type":"agent_message","text":"..."}}` — the
    model's message(s); the review report is the **last** `agent_message` item's text.
  - `{"type":"turn.completed",...}` — success.
  - `{"type":"turn.failed","error":{"message":"..."}}` — failure; `.error.message` is the
    diagnostic. Confirmed via a deliberately invalid model id (exit code 1, `turn.failed`
    emitted with a JSON-encoded provider error string).
- **Resume**: `codex exec resume <SESSION_ID> [PROMPT] --json -m <model>`. The `resume`
  subcommand does not accept `-s/--sandbox` — it inherits the sandbox mode from the
  original session, which is `read-only` since that's how the session was started.

## Components

### 1. `scripts/cx-delegate.sh`

The delegate script, structurally identical to `cr-delegate.sh` (same output discipline:
**only the final STATUS JSON goes to stdout; all diagnostics/progress go to stderr**).

**Arguments** (identical surface to `cr-delegate.sh`):
- `--target spec|plan` (required)
- `--doc-file <path>` (required)
- `--spec-file <path>` (optional, for `plan` reviews)
- `--lenses backend,frontend,ui` (optional, for `spec` reviews)
- `--rubric-dir <path>` (defaults to `${CLAUDE_PLUGIN_ROOT}/rubrics` — the same directory
  the Cursor reviewer uses; no new rubric files)
- `--session <id>` (optional, resumes a prior Codex thread)

**Environment overrides** (mirrors `CR_CURSOR_BIN` / `CR_MODELS_JSON`):
- `CX_CODEX_BIN` (default `codex`)
- `CX_MODELS_JSON` (default `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/models.json`)

**Behavior:**
1. Validate args exactly as `cr-delegate.sh` does (unknown/missing args exit 2; unreadable
   `--doc-file`/`--spec-file` exits 2; unknown `--target` exits 2; unknown lens exits 2;
   missing rubric files exit 2).
2. Read `.codex_reviewer.id` from `CX_MODELS_JSON` via `jq`; missing/empty exits 2.
3. Assemble the prompt from the **same rubric files** as `cr-delegate.sh` (role preamble +
   target rubric + selected lenses + `_output-format.md` + instruction to explore the
   existing repo + pointers to `--doc-file`/`--spec-file`).
4. Run:
   ```
   codex exec --json -s read-only -m "$MODEL" "$PROMPT" < /dev/null
   ```
   or, when `--session` is given:
   ```
   codex exec resume "$SESSION" --json -m "$MODEL" "$PROMPT" < /dev/null
   ```
   Render non-final `item.completed` events (and their tool-call equivalents) to stderr as
   progress. Capture `thread.started.thread_id` as the session id.
5. Determine the report: the **last** `item.completed` event whose `item.type` is
   `"agent_message"`, taking `.item.text`.
6. Treat as failure (→ `BLOCKED`): non-zero exit, a `turn.failed` event present, no
   `agent_message` item found, or an empty report text.
7. Emit one JSON line:
   `{"status":"REVIEWED"|"BLOCKED","session_id":...,"target":...,"lenses":...,"report":...,"diagnostic":...}`.

**No verify loop, no commit, no retries** — same as `cr-delegate.sh`: one read-only pass;
`session_id` is returned so the controller can dispatch a resumed follow-up.

### 2. `agents/codex-reviewer-delegator.md`

Haiku subagent, `tools: Bash, Read`. Same contract as `cursor-reviewer-delegator`, inverted
for Codex:

- Receives in its prompt: `target`, doc path, optional spec path, lenses, optional session
  id to resume.
- Runs `cx-delegate.sh`, parses the JSON with `jq`, relays the `report` **verbatim** plus
  `session_id` and `status`.
- **Hard contract:** never edits files, never writes code, never decides whether a finding
  is valid, never filters/reorders findings, never fabricates a report — a script `BLOCKED`
  is reported as `BLOCKED` with the diagnostic, and that's the end of its job.
- Report format: `Status` (REVIEWED | BLOCKED), the verbatim review, `session_id` (required,
  copied verbatim), and any script diagnostic on BLOCKED.

### 3. `commands/codex-review.md`

`/codex-review <spec|plan> <doc-path> [spec-path]`. The controller flow, mirroring
`/cursor-review`:

1. **Preflight:** require a target and doc path; confirm the doc exists; run a real headless
   probe:
   ```
   CODEX_REVIEWER_MODEL=$(jq -er '.codex_reviewer.id // empty' "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/models.json")
   codex exec --json -s read-only -m "$CODEX_REVIEWER_MODEL" "Reply with the single word READY." < /dev/null
   ```
   On any failure (non-zero exit, `turn.failed`, no `READY` in the final `agent_message`),
   tell the user to run `codex login` (suggest they type `! codex login`) and stop.
2. **Lens detection (spec only):** identical logic to `/cursor-review` — backend always;
   frontend/ui when the spec shows UI surface; ask the user once if ambiguous.
3. **Dispatch** the `codex-reviewer-delegator` subagent (not a general-purpose one) with
   target/doc/spec/lenses/(optional) session id.
4. **Triage:** apply `superpowers:receiving-code-review` — verify each finding against the
   codebase, push back with reasoning where wrong, accept and fix where right.
5. **Present** triaged findings to the user; offer a fix → re-review loop (resume via the
   returned `session_id`).
6. **Review Effectiveness Summary** (always): print it and append a dated entry to
   `docs/cursor-reviewer/effectiveness-log.md` (same shared log as the Cursor reviewer, so
   effectiveness across both reviewers is comparable over time) — target, doc, lenses,
   reviewer used (**Codex/GPT-5.6 Sol**), finding counts by severity, triage outcome,
   environment friction, recommendations.

### 4. `rubrics/`

**No changes.** `spec-review.md`, `plan-review.md`, `lens-backend.md`, `lens-frontend.md`,
`lens-ui.md`, and `_output-format.md` are provider-agnostic criteria text already; both
reviewers share them unmodified.

### 5. `.claude-plugin/models.json`

Add a third top-level key; the existing `reviewer` key (Cursor/Grok) is untouched:

```json
{
  "coder": { "id": "composer-2.5", "label": "Composer 2.5" },
  "reviewer": { "id": "cursor-grok-4.5-high-fast", "label": "Grok 4.5 (high effort, fast)" },
  "codex_reviewer": { "id": "gpt-5.6-sol", "label": "GPT-5.6 Sol" }
}
```

### 6. `scripts/sync-models.sh`

Extended, following its existing explicit-per-role style (no generalization into a loop —
consistent with how `coder`/`reviewer` are already handled, and lower-risk than a refactor):

- Read and validate `CODEX_REVIEWER_ID` / `CODEX_REVIEWER_LABEL` from `.codex_reviewer.id`/
  `.label`, same charset/emptiness checks as the other two roles.
- Update `PLUGIN_DESC` / `MARKETPLACE_DESC` to name all three: Composer 2.5 (implementation),
  Grok 4.5 (review via cursor-agent), GPT-5.6 Sol (review via Codex CLI).
- New frontmatter regen call for `agents/codex-reviewer-delegator.md` and
  `commands/codex-review.md` (description lines mentioning GPT-5.6 Sol).
- New `MARKER_TARGETS` entries: `README.md:codex_reviewer`,
  `agents/codex-reviewer-delegator.md:codex_reviewer`,
  `commands/codex-review.md:codex_reviewer`, `scripts/cx-delegate.sh:codex_reviewer`,
  `tests/e2e-smoke.md:codex_reviewer`.

### 7. `README.md`

- Third row in the subagent/command table:
  `codex-reviewer-delegator` | `/codex-review <spec|plan> <doc-path> [spec-path]` | GPT-5.6
  Sol (read-only) | Runs the Codex delegate script and relays the report verbatim.
- Requirements: `codex` CLI installed and logged in (`codex login status` / `codex login`).
- Usage section: `/codex-review spec <spec-path>` / `/codex-review plan <plan-path>
  <spec-path>` examples alongside the existing Cursor ones.
- Explicit statement of the **ask-which-reviewer convention**: at the brainstorming Spec
  self-review gate and the writing-plans Self-Review gate, ask the user whether to use the
  Cursor/Grok reviewer or the Codex/GPT-5.6 Sol reviewer before running either command.

### 8. Plumbing

- `.claude-plugin/plugin.json` — description regenerated by `sync-models.sh` to mention all
  three models; `keywords` gains `codex`; `version` bumped per the normal release flow (not
  part of this spec — happens at release time).
- `Makefile` — no changes; existing `bump-*`/`release` targets apply unchanged.

## Data flow

```
/codex-review spec docs/.../spec.md
  → preflight (READY probe via codex exec -s read-only, doc exists)
  → lens detection (backend [+frontend +ui])
  → dispatch codex-reviewer-delegator(target, doc, lenses)
      → cx-delegate.sh assembles prompt (rubric + lenses + output-format)
      → codex exec --json -s read-only -m gpt-5.6-sol  (reads doc + explores repo)
      → emits {status, session_id, target, lenses, report}
  → delegator relays report + session_id verbatim
  → Opus triages via superpowers:receiving-code-review
  → present to user → optional fix → re-review (resume session_id)
  → Review Effectiveness Summary (print + append to effectiveness-log.md)
```

At a self-review gate (not an explicit `/codex-review` or `/cursor-review` invocation), the
controller first asks the user: "Cursor/Grok reviewer or Codex/GPT-5.6 Sol reviewer?" — then
runs the corresponding command as above.

## Error handling

| Condition | Outcome |
|-----------|---------|
| `codex` unauth / no `READY` / non-zero exit on preflight | Preflight stops; user runs `codex login`. |
| Missing/unreadable doc | Preflight stops. |
| Non-zero exit / no `agent_message` item / malformed JSONL | Script `BLOCKED` with diagnostic. |
| `turn.failed` event | Script `BLOCKED` with `.error.message`. |
| Empty report | Script `BLOCKED` — never fabricated. |
| Delegator sees BLOCKED | Reports BLOCKED verbatim; never improvises a review. |
| Reviewer attempts a file edit | Impossible: `-s read-only` sandbox **and** delegator has no Write/Edit tools. |

## Testing strategy

- **Unit** (`tests/test-cx-delegate.sh` against `tests/mock-codex`, mirroring
  `test-cr-delegate.sh`): arg validation and exit codes; model resolution from
  `.codex_reviewer.id` (including `CX_MODELS_JSON` override, missing file, missing field);
  REVIEWED happy path with captured `session_id` (from `thread.started`) and `report` (from
  the last `agent_message`); BLOCKED on CLI failure, on a `turn.failed` event, and on an
  empty report; correct lens rubric assembly into the prompt; presence of `-s read-only` and
  absence of a hardcoded model literal in the invoked command line; `--session` maps to
  `codex exec resume <session>`.
- **`tests/mock-codex`**: a stand-in for the `codex` binary, controllable via env vars
  (mirroring `mock-cursor-agent`'s `MOCK_SESSION`, `MOCK_RESULT`, `MOCK_FAIL_CLI`,
  `MOCK_STDERR`, `MOCK_LOG`) to emit the JSONL shapes documented above (`thread.started`,
  `item.completed`/`agent_message`, `turn.completed`, `turn.failed`).
- **`tests/test-sync-models.sh`**: extended to cover the third role — regeneration of the
  new marker targets and frontmatter descriptions, and validation failures for a missing/
  invalid `.codex_reviewer.id`/`.label`.
- **`tests/test-drift-coverage.sh`**: extended with `no_literal_model` / jq-read checks for
  `cx-delegate.sh`, `commands/codex-review.md`, and `tests/e2e-smoke.md` against the
  `gpt-5.6-sol` literal, matching the existing coder/reviewer guards.
- **E2E smoke** (`tests/e2e-smoke.md`): new section — manual run of `/codex-review spec` and
  `/codex-review plan` against a sample doc with a real logged-in `codex`, confirming a
  structured report comes back and no files were modified.

## Open items / future

- **Combined dual-review command:** explicitly out of scope now; if useful later, a command
  that dispatches both delegators and presents both reports side by side is a natural
  follow-on.
- **Visual UI review:** out of scope, as with the existing reviewer — would need a
  screenshot step feeding either reviewer.
