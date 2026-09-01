---
name: cursor-reviewer
description: Use when reviewing a design spec or implementation plan with Cursor.
version: 1.0.0
metadata:
  hermes:
    tags: [cursor, review, delegation]
    requires_toolsets: [terminal, file]
---

# Cursor Reviewer

<!-- ADAPTER-PARITY: commands/review.md; policy: docs/adapter-parity.md -->

Accept only `spec` or `plan` targets. Resolve and preserve one run ID. Before
executing any review workflow, load and confirm the runtime Superpowers
`receiving-code-review` skill is available; stop with a clear diagnostic if it
cannot be loaded.

Execute `${HERMES_SKILL_DIR}/../../scripts/dispatch.py probe --role reviewer
--run-id <run-id>` before review. Execute `dispatch.py review` with the document paths, target,
lenses, run ID, latest generation, and prior Cursor session ID on re-review.
Treat any non-REVIEWED result, empty report, empty session ID, or snapshot
mutation as BLOCKED. Relay the report verbatim, then use
`receiving-code-review` to verify and triage findings. When the controller
closes the review cycle, call `dispatch.py state --action complete` with the
latest generation so the persisted reviewer record transitions
`reviewed -> complete` and remains as an idempotent tombstone until the 30-day
prune. Append the host-tagged
effectiveness record only after the worker-mutation check. The retained Cursor
MCP approval is an accepted trust boundary and not an operating-system sandbox.

The concrete probe command is:

```bash
"${HERMES_SKILL_DIR}/../../scripts/dispatch.py" probe \
  --role reviewer \
  --run-id "$RUN_ID"
```

## Targets and lenses

- Accept only design specification or implementation plan review.
- For `spec` reviews, derive lenses exactly as `commands/review.md`: always
  include `backend`; add `frontend` when the spec describes UI components,
  client state, routes, screens, or styling; add `ui` when the spec describes
  user-facing flows, layouts, or UX.
- For `plan` reviews, do not pass lenses.

## Workflow

1. Require a git repository with a valid `HEAD` and a readable document inside
   it. For plan review, apply the same rule to the optional specification.
2. Generate one cryptographically random 16-character lowercase-hex run ID at
   workflow start and preserve it for probe, review, re-review, recovery, and
   final completion.
3. Probe the configured reviewer model with a real call before the first review
   and before any resumed re-review after environment changes.
4. Dispatch review with `dispatch.py review`, passing `--repo` when the
   controller checkout is not the current working directory.
5. On initial review, omit `--expected-generation`. On re-review, pass the
   latest generation and the prior Cursor session ID.
6. Treat `status: REVIEWED` as success only when `report` and `session_id` are
   both non-empty and `snapshot_path` is empty.
7. Relay the report verbatim to the user.
8. Apply `receiving-code-review` to verify and triage findings.
9. After confirming the controller repository was not mutated by the review
   worker, append a host-tagged entry to
   `docs/cursor-reviewer/effectiveness-log.md`.
10. When the review cycle is finished, run
    `dispatch.py state --action complete --run-id <run-id>
    --expected-generation <latest-generation>`. Completion is explicitly
    idempotent: retrying with the generation returned by the complete tombstone
    returns that same tombstone. Missing state never fabricates a generation.

## Trust boundary

The shared Cursor harness retains `--force --trust --approve-mcps --mode ask`.
Reviewer mode is read-only for repository files, but MCP approval is an
accepted trust boundary and not an operating-system sandbox.
