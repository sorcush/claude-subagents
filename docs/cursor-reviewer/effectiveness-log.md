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

## 2026-08-22 — reviewer/coder pools design spec

- **Reviewer:** Codex / GPT-5.6 Sol (`cx-delegate.sh`, one round, session
  `01a02ba1-f2e0-7dc1-9ae7-417d084ecd29`).
- **Run:** 2026-08-22 · target `spec` ·
  `docs/superpowers/specs/2026-08-22-reviewer-coder-pools-design.md` ·
  lens `backend`.
- **Findings:** 5 Critical, 11 Important, 4 Minor. Verdict: **Needs revision**.
- **Triage outcome:** 19 of 20 accepted and fixed in one revision. One rejected:
  the request for aliases or a migration path for the three removed commands —
  this plugin has a single author and user, and keeping `/cursor-review` alive
  would preserve exactly the tool-specific naming the change removes. One
  accepted with a corrected rationale: the reviewer framed over-broad symlinking
  as a secrets-exposure problem, but read access to `.env` is pre-existing (the
  coder already runs inside the repo today); the genuinely new defect was the
  *write* path back into the main checkout through the link.
- **Reviewer quality:** high, and codebase-grounded rather than generic. It
  read `cc-delegate.sh`, `cr-delegate.sh` and `cx-delegate.sh` and derived
  regression risks from the actual current code — most valuably that
  `cc-delegate.sh` runs `eval "$VERIFY_CMD"` in the caller's directory, so a
  mechanical port would verify the main checkout instead of the new worktree.
  Its best finding was structural, not textual: the spec's end-of-run flow said
  "merge into the branch the run started from", which is incoherent when the run
  starts *on* the feature branch. That pointed at a bigger error — the design
  was reimplementing `superpowers:finishing-a-development-branch`, which already
  determines the base branch and offers merge/PR/keep/discard. Deleting the
  hand-written flow removed the defect and a whole block of would-be code.
- **Verified, not assumed:** two claims were checked against the real CLIs
  before being accepted. `codex exec resume --help` confirms there is no `-C`
  and no `-s/--sandbox`, so a resumed review really would lose its read-only
  sandbox — a genuine safety hole, fixed with `-c sandbox_mode="read-only"`.
  Reading the superpowers skill confirmed the duplication above. No false
  positives found.
- **Environment friction:** none. `codex exec` ran first time; the review took
  three tool-call rounds and returned a well-formed report and session id.
- **Recommendations:** (1) When a spec proposes new orchestration steps, check
  first whether an installed superpowers skill already covers them — the
  reviewer found this by accident via a wording bug, and a rubric line asking
  "does an existing skill already do this?" would find it directly. (2) The
  reviewer flagged several places where the spec described a shell contract in
  prose (`harness_run` sets variables, scripts emit one JSON line) without
  stating the constraint that makes it work (must not be called in a subshell,
  must not write to stdout). Worth adding to `lens-backend.md`: for any shell
  interface, require the calling convention to be written down, not implied.

## 2026-08-22 — reviewer/coder pools design spec, round 2

- **Reviewer:** Codex / GPT-5.6 Sol, resuming session
  `01a02ba1-f2e0-7dc1-9ae7-417d084ecd29` so it could grade its own round-1
  findings.
- **Run:** 2026-08-22 · target `spec` · same doc · lens `backend`.
- **Findings:** graded the 20 prior findings (16 FIXED, 4 PARTIALLY FIXED), then
  1 new Critical, 4 Important, 2 Minor. Verdict: **Needs revision**.
- **Triage outcome:** all 7 new findings accepted; all 4 partials accepted.
  Nothing rejected this round. The reviewer also revisited both round-1
  disagreements unprompted: it **accepted** the rejection of command aliases,
  and **agreed** with the correction that `.env` read access was pre-existing
  and the real defect was the write path — while correctly refusing to let that
  agreement excuse the write path itself.
- **Reviewer quality:** the single best finding of either round was a
  self-contradiction I had written and not seen: success criterion 4 promised
  the coder never writes into the main checkout, while the linking section gave
  it writable symlinks to `node_modules` and build caches. It named both line
  ranges and refused three times to accept a narrowed allowlist as a fix,
  because narrowing the list does not close a write path. That was correct and
  I had twice failed to see it. Resolved with copy-on-write cloning, which the
  reviewer had not proposed — it offered "copy, mount read-only, or weaken the
  claim", and `cp -Rc` / `cp --reflink` is the option that keeps the guarantee
  at near-zero cost (measured: 500 files in 0.27s).
- **Resume worked, and proved the fix.** Round 1's Critical #5 was that
  `codex exec resume` silently drops `-s read-only`. Verified against the CLI:
  no `-C`, no `-s`. This user's `~/.codex/config.toml` sets no `sandbox_mode`
  and marks `/Users/sandrey/Dev` as `trusted`, so a resumed reviewer really
  would have had write access. Round 2 was run with the prescribed fix,
  `-c sandbox_mode="read-only"`, applied by hand — full context retained, zero
  writes. The fix is confirmed in practice, not only on paper.
- **Environment friction:** none, once the sandbox flag was added by hand. Note
  this means the shipped `cx-delegate.sh` resume path should not be used for
  further rounds until it is fixed.
- **Recommendations:** (1) Resuming the reviewer's session is clearly worth it
  for a revision round — grading its own findings caught two places where I had
  fixed the symptom and not the cause, which a fresh reviewer would have had no
  reason to look for. Make session resume the default for re-review, not the
  exception. (2) Add to `lens-backend.md`: when a document states a guarantee,
  check every mechanism elsewhere in the document against it. Both rounds' worst
  findings were internal contradictions between a promise and an implementation
  detail, not defects in either one alone.

## 2026-08-22 — reviewer/coder pools implementation plan (round 3)

- **Reviewer:** Codex / GPT-5.6 Sol, resuming session
  `01a02ba1-f2e0-7dc1-9ae7-417d084ecd29` for the third time, now reviewing the
  plan against the spec it had already shaped.
- **Run:** 2026-08-22 · target `plan` · plan vs spec · plan-review rubric.
- **Findings:** 4 Critical, 12 Important, 4 Minor. Verdict: **Needs revision**.
- **Triage outcome:** 19 of 20 accepted. One rejected on the facts (below).
- **The finding that justified the whole round.** The plan's copy-on-write
  cloning did NOT deliver the isolation the spec promises. `cp -R` and `cp -Rc`
  preserve symlinks rather than following them, so an absolute link inside a
  copied `node_modules` still points at the main checkout. Verified directly:
  planted such a link, wrote through it from the worktree, and the main-checkout
  file changed to "PWNED". This is the **third consecutive round** in which the
  same guarantee leaked by a different mechanism — link everything ignored, then
  link a narrower list, then copy but keep the links inside.
- **Tests that proved nothing.** The reviewer's sharpest structural point was
  that several tests could not fail. The isolation test asserted
  `find -type l` finds zero links, but its fixture contained no symlink. The
  removal test created `untracked-user-file.txt` and then asserted removal
  *succeeded* — baking in the destructive `--force` + `rm -rf` behavior as
  correct. The probe test's only negative case was lowercase `"not ready"`, so it
  could not catch `grep -q READY` accepting `NOTREADY`.
- **Rejected, with evidence:** it called `out="$(cmd)"; [[ $? -eq 0 ]]` a Critical
  correctness bug. It is not — bash sets `$?` from the command substitution;
  verified with a function returning 7. Severity wrong, advice still worth taking,
  so the explicit form was adopted anyway.
- **Found independently, not by the reviewer:** the codex probe used
  `grep -q '"type":"turn.failed"'` on raw output. My own identical grep misfired
  on *this review's* output, because the reviewer's prose contained that string.
  Same substring class as the READY bug; now parsed with `jq`.
- **Environment friction:** none. Third resume with
  `-c sandbox_mode="read-only"` applied by hand; full context retained, no writes.
- **Recommendations:** (1) Add to `plan-review.md`: "for each test, name an input
  that would make it fail. If you cannot, the test proves nothing." Three of this
  round's best findings were unfailable tests, and no current rubric line asks for
  that. (2) Add to `lens-backend.md`: "a stated guarantee needs a test that
  attacks it, not prose that restates it." Three rounds, three leaks of the same
  guarantee, each caught only after it was written down as safe.
