# Cursor Reviewer Effectiveness Log

## 2026-07-29 — spec review, 1 round

- **Run:** 2026-07-29 · target: spec · doc:
  `docs/superpowers/specs/2026-07-29-codex-reviewer-design.md` · lenses: backend ·
  session `7f57533e-0462-43b0-9c8e-98402b8f7bac`.
- **Findings:** 2 Critical, 6 Important, 5 Minor, verdict "Approve with fixes."
- **Triage outcome:** accepted both Critical findings and 5 of 6 Important findings
  in full. Partial pushback on 1 Important finding: the reviewer called the
  README-only "ask which reviewer" gate convention a contradiction with the
  spec's own out-of-scope line ("changing the existing reviewer's behavior");
  it isn't — this repo's original cursor-reviewer design already established
  that superpowers-gate integration is doc-only convention, not skill/code
  edits. Accepted the actionable half of the suggestion (a one-line
  discoverability pointer added to both command files) without treating it as
  a contradiction to resolve. All 5 Minor findings accepted as written.
- **Reviewer quality:** both Critical findings were specific and
  codebase-grounded — it read the actual installed `codex --help` surface (not
  assumed) to catch the missing `-C/--cd` working-root spec, and it read
  `scripts/sync-models.sh`'s real `regen_markers` function to catch that its
  binary `if/else` would silently write the wrong label into a third role's
  marker spans. No false positives among the Critical/Important findings. The
  "contradiction" framing on the gate-convention finding was the one
  overreach — a plausible-sounding claim that didn't hold up against what the
  spec's own Scope section already said.
- **Environment friction:** none. Preflight probe returned READY immediately;
  the dispatched review completed in one round with a clean REVIEWED status
  and a non-empty `session_id`.
- **Recommendations:** none for the rubrics/dispatch prompt this round — the
  finding quality suggests the existing `spec-review.md` + `lens-backend.md`
  rubric combination is well-tuned for infra/plumbing specs like this one.

## 2026-07-18 — spec review, 4 rounds

- **Run:** 2026-07-18 · target: spec · doc:
  `docs/superpowers/specs/2026-07-18-changelog-and-model-config-design.md` ·
  lenses: backend · session `732efd68-22ed-4880-9f1b-22fa18475a00` (resumed
  across all 4 rounds).
- **Findings:** round 1: 2 Critical, 9 Important, 4 Minor, verdict "Needs
  revision." Round 2: 1 Critical, 9 Important, 4 Minor, verdict "Approve
  with fixes." Round 3: 1 Critical, 6 Important, 4 Minor, verdict "Approve
  with fixes." Round 4: 1 Critical, 5 Important, 3 Minor, verdict "Approve
  with fixes" (no Critical left unaddressed after this round's fix).
- **Triage outcome:** accepted essentially every Critical/Important finding
  across all 4 rounds (only pushback: the "Approved (pending user spec
  review)" status-line phrasing, which is this repo's existing convention
  from a prior spec, not a real contradiction — repeated by the reviewer in
  rounds 1–4 without new argument each time). Two of the four rounds' fixes
  introduced their own new defects that the *next* round caught (round 2's
  fix put HTML markers inside copy-pasteable shell argv in
  `tests/e2e-smoke.md`; round 3's fix used "prepend" ambiguously, which
  round 4 caught as a literal byte-0 insertion bug that would corrupt
  `CHANGELOG.md`'s structure on the second release onward).
- **Reviewer quality:** consistently specific and codebase-grounded — every
  major finding cited an exact file/line and, where relevant, quoted the
  actual current text (e.g. it independently spotted that `keywords` in
  `plugin.json` still said `"gpt-5.5"` in round 1, and in round 4 caught
  that "prepend" would literally place a new version section above the
  `# Changelog` title). No false positives identified. It did re-flag the
  same status-line "issue" in every round despite being told it's
  intentional — the only recurring low-value finding.
- **Environment friction:** none — `cursor-agent` healthcheck passed on
  first probe; every round's `--session` resume worked cleanly.
- **Recommendations:** for specs with this much interlocking mechanical
  detail (idempotency guards, insertion points, marker inventories), budget
  for 3-4 review rounds up front rather than expecting one pass — each
  round's fix was itself a small, reviewable diff, and the reviewer
  reliably caught regressions introduced by the previous round's own fix.
  Consider having the reviewer's rubric explicitly deprioritize a finding
  it already raised and was told is intentional, to reduce repeated Minor
  noise across rounds.
