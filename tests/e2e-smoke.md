# E2E Smoke Tests (manual — real cursor-agent)

Prereqs: `cursor-agent status` shows logged in.

## Setup
```bash
tmp=$(mktemp -d)
cd "$tmp"
git init -q && git commit -q --allow-empty -m "init"
printf 'Create a file calc.py with a function add(a, b) that returns a+b. Then create test_calc.py with a pytest test asserting add(2,3)==5.\n' > task.md
```

## 1. Happy path (Composer implements, verify passes)
```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/cc-delegate.sh" \
  --task-file "$tmp/task.md" \
  --verify-cmd "python -m pytest -q"
```
Expect: stdout is one JSON line with `"status":"DONE"`, `"verified":true`,
`"attempts":0`. `calc.py` and `test_calc.py` now exist; `pytest` passes.

## 2. Resume fix-loop (force one failure)
```bash
# A verify command that fails the first time, passes after the file is corrected.
printf 'Create greet.py with greet() returning the string "hi".\n' > task2.md
bash "$CLAUDE_PLUGIN_ROOT/scripts/cc-delegate.sh" \
  --task-file "$tmp/task2.md" \
  --verify-cmd 'python -c "import greet; assert greet.greet()==\"hi\""' \
  --max-retries 3
```
Expect: `"status":"DONE"`, `"verified":true`. If Composer's first attempt is
already correct, `attempts` may be 0 — that's fine; the loop is exercised by test 3.

## 3. BLOCKED path (impossible verify)
```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/cc-delegate.sh" \
  --task-file "$tmp/task.md" \
  --verify-cmd 'exit 1' \
  --max-retries 2
```
Expect: `"status":"BLOCKED"`, `"verified":false`, `"attempts":2`, exit code 1.

## 4. Preflight failure (logged out) — optional
Temporarily move auth or run in an environment without login; confirm
`/cursor-implement-plans` stops at preflight with a clear message.

## Cleanup
```bash
rm -rf "$tmp"
```

---

# Reviewer delegation (cr-delegate.sh — <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->)

Prereqs: `cursor-agent login` done; the probe below returns `READY`:
```bash
REVIEWER_MODEL=$(jq -er '.reviewer.id // empty' "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/models.json")
if [[ -z "$REVIEWER_MODEL" ]]; then
  echo "error: could not read .reviewer.id from models.json"; exit 2
fi
cursor-agent -p --force --trust --mode ask --model "$REVIEWER_MODEL" "Reply READY."
```

## Setup
```bash
tmp=$(mktemp -d); cd "$tmp"
git init -q && git commit -q --allow-empty -m "init"
cat > spec.md <<'EOF'
# Widget Cache Design
Goal: add an in-memory cache for widget lookups.
Architecture: a singleton map keyed by widget id, no eviction.
EOF
```

## R1. Spec review (happy path)
```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/cr-delegate.sh" \
  --target spec --doc-file "$tmp/spec.md" --lenses backend
```
Expect: stdout is ONE JSON line with `"status":"REVIEWED"`, a non-empty `"report"`,
a real `"session_id"`, and `"lenses":["backend"]`. The report should follow the
output format (Summary / Strengths / Issues / Verdict) and likely flag the "no
eviction" unbounded-growth risk. No files in `$tmp` were modified.

## R2. Plan review against a spec
```bash
cat > plan.md <<'EOF'
# Widget Cache Implementation Plan
Task 1: add cache.py with get(id) and set(id, val).
EOF
bash "$CLAUDE_PLUGIN_ROOT/scripts/cr-delegate.sh" \
  --target plan --doc-file "$tmp/plan.md" --spec-file "$tmp/spec.md"
```
Expect: `"status":"REVIEWED"`, `"target":"plan"`; the report should note missing
eviction coverage / thin task decomposition relative to the spec.

## R3. BLOCKED path (logged out)
Temporarily log out (or unset auth) and re-run command R1. Expect `"status":"BLOCKED"`,
exit code 1, and a `diagnostic` explaining the auth/trust failure. No fabricated review.

## Cleanup
```bash
rm -rf "$tmp"
```

---

# Codex reviewer delegation (cx-delegate.sh — <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->)

Prereqs: `codex login status` shows logged in; the probe below returns `READY`:
```bash
CODEX_REVIEWER_MODEL=$(jq -er '.codex_reviewer.id // empty' "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/models.json")
if [[ -z "$CODEX_REVIEWER_MODEL" ]]; then
  echo "error: could not read .codex_reviewer.id from models.json"; exit 2
fi
codex exec --json -s read-only -m "$CODEX_REVIEWER_MODEL" "Reply with the single word READY." < /dev/null
```

## Setup
```bash
tmp=$(mktemp -d); cd "$tmp"
git init -q && git commit -q --allow-empty -m "init"
cat > spec.md <<'EOF'
# Widget Cache Design
Goal: add an in-memory cache for widget lookups.
Architecture: a singleton map keyed by widget id, no eviction.
EOF
```

## R1. Spec review (happy path)
```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target spec --doc-file "$tmp/spec.md" --lenses backend
```
Expect: stdout is ONE JSON line with `"status":"REVIEWED"`, a non-empty `"report"`,
a real `"session_id"`, and `"lenses":["backend"]`. The report should follow the
output format (Summary / Strengths / Issues / Verdict) and likely flag the "no
eviction" unbounded-growth risk. No files in `$tmp` were modified.

## R2. Plan review against a spec
```bash
cat > plan.md <<'EOF'
# Widget Cache Implementation Plan
Task 1: add cache.py with get(id) and set(id, val).
EOF
bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target plan --doc-file "$tmp/plan.md" --spec-file "$tmp/spec.md"
```
Expect: `"status":"REVIEWED"`, `"target":"plan"`; the report should note missing
eviction coverage / thin task decomposition relative to the spec.

## R3. BLOCKED path (logged out)
Temporarily log out (or unset auth) and re-run command R1. Expect `"status":"BLOCKED"`,
exit code 1, and a `diagnostic` explaining the failure. No fabricated review.

## R4. Resume across rounds
```bash
SESSION=$(bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target spec --doc-file "$tmp/spec.md" --lenses backend | jq -r .session_id)
bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target spec --doc-file "$tmp/spec.md" --lenses backend --session "$SESSION"
```
Expect: second call also returns `"status":"REVIEWED"` and the same `"session_id"`,
demonstrating `codex exec resume` picks the thread back up.

## Cleanup
```bash
rm -rf "$tmp"
```
