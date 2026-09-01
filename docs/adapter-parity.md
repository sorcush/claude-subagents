# Adapter Parity

Hermes and Claude intentionally duplicate controller workflows while sharing
the deterministic shell harnesses.

## Mappings

- `commands/implement-plans.md` ↔ `hermes/skills/cursor-coder/SKILL.md`
- `agents/coder-delegator.md` ↔ `hermes/scripts/dispatch.py`
- `commands/review.md` ↔ `hermes/skills/cursor-reviewer/SKILL.md`
- `agents/reviewer-delegator.md` ↔ `hermes/scripts/dispatch.py`

## Classification

Shared behavior includes worktree safety, verification and session-ID
requirements, retry/resume policy, controller/worker boundaries, rubric
selection, failure classification, integration, and cleanup.

Claude-specific behavior includes `$ARGUMENTS`, `${CLAUDE_PLUGIN_ROOT}`, custom
delegator agents, Claude tool allowlists, and Claude command names.

Hermes-specific behavior includes `${HERMES_SKILL_DIR}`, native registration,
`hermes config get`, Hermes skill loading and todos, profile-scoped settings,
durable run state, and Hermes terminal/background-process behavior.

## Update procedure

Before changing a mapped adapter, inspect its counterpart, classify the change,
update both sides for shared behavior, record intentional divergence below, and
run the Claude and Hermes contract suites.

## Deliberate divergences

- Hermes uses profile configuration; Claude uses pool JSON.
- Hermes calls delegates directly; Claude uses restricted delegator agents.
- Hermes coder uses run-scoped worktrees; Claude retains legacy deterministic naming.
- Hermes coder enables Cursor sandbox and a disposable verification home; Claude retains current defaults.
- Hermes reviewer uses an independent clone; Claude reviews the current checkout read-only.
