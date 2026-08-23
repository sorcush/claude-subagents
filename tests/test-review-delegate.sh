#!/usr/bin/env bash
# Unit tests for scripts/review-delegate.sh across all three harnesses.
# Run: bash tests/test-review-delegate.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/review-delegate.sh"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}
has() {
  local desc="$1"; shift
  [[ "${1:-}" == "--" ]] && shift
  check "$desc" "1" "$([ "$(grep -c -F -- "$1" "$2")" -ge 1 ] && echo 1 || echo 0)"
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export CSC_CURSOR_BIN="$HERE/mock-cursor-agent"
export CSC_CODEX_BIN="$HERE/mock-codex"
export CSC_CLAUDE_BIN="$HERE/mock-claude"

cat > "$TMP/reviewers.json" <<'EOF'
{"reviewers":[
  {"key":"r-cursor","label":"Cursor","harness":"cursor","model":"m-cursor"},
  {"key":"r-codex","label":"Codex","harness":"codex","model":"m-codex"},
  {"key":"r-claude","label":"Claude","harness":"claude","model":"m-claude"}]}
EOF
export CSC_REVIEWERS_JSON="$TMP/reviewers.json"

# Stub rubrics with sentinels so prompt assembly can be asserted.
RUBRICS="$TMP/rubrics"; mkdir -p "$RUBRICS"
echo "SPEC_RUBRIC_SENTINEL"   > "$RUBRICS/spec-review.md"
echo "PLAN_RUBRIC_SENTINEL"   > "$RUBRICS/plan-review.md"
echo "BACKEND_LENS_SENTINEL"  > "$RUBRICS/lens-backend.md"
echo "FRONTEND_LENS_SENTINEL" > "$RUBRICS/lens-frontend.md"
echo "UI_LENS_SENTINEL"       > "$RUBRICS/lens-ui.md"
echo "OUTPUT_FORMAT_SENTINEL" > "$RUBRICS/_output-format.md"

REPO=$(cd "$(mktemp -d)" && pwd -P)
(cd "$REPO" && git init -q && git commit -q --allow-empty -m init)
doc="$REPO/spec.md"; echo "# Some Spec" > "$doc"

run() { bash "$SCRIPT" --doc-file "$doc" --rubric-dir "$RUBRICS" "$@"; }

# --- happy path on every harness ---
for k in r-cursor r-codex r-claude; do
  out=$(MOCK_RESULT="the review" run --reviewer "$k" --target spec 2>/dev/null)
  check "$k reports REVIEWED"    "REVIEWED"   "$(echo "$out" | jq -r '.status')"
  check "$k echoes reviewer key" "$k"         "$(echo "$out" | jq -r '.reviewer')"
  check "$k returns the report"  "the review" "$(echo "$out" | jq -r '.report')"
  check "$k returns a session"   "1" \
    "$([[ -n "$(echo "$out" | jq -r '.session_id')" ]] && echo 1 || echo 0)"
  check "$k emits one JSON line" "1" "$(echo "$out" | wc -l | tr -d ' ')"
done

# --- prompt assembly ---
log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target spec --lenses backend,ui >/dev/null 2>&1
has "spec rubric included"    "SPEC_RUBRIC_SENTINEL"   "$log"
has "backend lens included"   "BACKEND_LENS_SENTINEL"  "$log"
has "ui lens included"        "UI_LENS_SENTINEL"       "$log"
has "output format included"  "OUTPUT_FORMAT_SENTINEL" "$log"
check "frontend lens absent"  "0" "$(grep -c -- 'FRONTEND_LENS_SENTINEL' "$log")"
check "plan rubric absent"    "0" "$(grep -c -- 'PLAN_RUBRIC_SENTINEL' "$log")"

# Order matters: target rubric, then lenses, then the output format. Presence alone
# would pass even if they were assembled in the wrong order. MOCK_LOG records the
# whole prompt as a single line, so compare character offsets within it.
argline=$(grep -m1 'ARGS:' "$log")
offset() { awk -v s="$argline" -v pat="$1" 'BEGIN { print index(s, pat) }'; }
p_rubric=$(offset SPEC_RUBRIC_SENTINEL)
p_lens=$(offset BACKEND_LENS_SENTINEL)
p_fmt=$(offset OUTPUT_FORMAT_SENTINEL)
check "rubric comes before the lens"        "1" "$([[ $p_rubric -gt 0 && $p_rubric -lt $p_lens ]] && echo 1 || echo 0)"
check "lens comes before the output format" "1" "$([[ $p_lens   -gt 0 && $p_lens   -lt $p_fmt  ]] && echo 1 || echo 0)"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target plan --spec-file "$doc" >/dev/null 2>&1
has "plan rubric included" "PLAN_RUBRIC_SENTINEL" "$log"
rm -f "$log"

# --- read-only enforcement, including on resume ---
log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target spec >/dev/null 2>&1
has "codex new call is read-only" -- "-s read-only" "$log"
rm -f "$log"

# The regression this guards: `codex exec resume` has no -s flag, so without
# the -c override a resumed review silently loses its read-only sandbox.
log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target spec --session prev-1 >/dev/null 2>&1
has "codex resume uses the resume subcommand" "resume prev-1" "$log"
has "codex resume forces read-only via -c"    'sandbox_mode="read-only"' "$log"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-cursor --target spec --session prev-2 >/dev/null 2>&1
has "cursor resume keeps --mode ask" -- "--mode ask" "$log"
has "cursor resume passes the session" "resume=prev-2" "$log"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-claude --target spec --session prev-3 >/dev/null 2>&1
has "claude resume stays read-only" "dontAsk" "$log"
rm -f "$log"

# --- failures ---
out=$(MOCK_FAIL_CLI=1 run --reviewer r-codex --target spec 2>/dev/null)
check "tool failure is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "diagnostic is populated" "1" \
  "$([[ -n "$(echo "$out" | jq -r '.diagnostic')" ]] && echo 1 || echo 0)"

out=$(MOCK_RESULT="" run --reviewer r-codex --target spec 2>/dev/null)
check "empty report is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"

MOCK_FAIL_CLI=1 run --reviewer r-codex --target spec >/dev/null 2>&1
check "BLOCKED exits 1" "1" "$?"

# --- argument validation ---
bash "$SCRIPT" --rubric-dir "$RUBRICS" >/dev/null 2>&1
check "no args exits 2" "2" "$?"
run --reviewer r-codex --target bogus >/dev/null 2>&1
check "bad target exits 2" "2" "$?"
run --reviewer r-codex --target spec --lenses nope >/dev/null 2>&1
check "unknown lens exits 2" "2" "$?"
run --reviewer nosuch --target spec >/dev/null 2>&1
check "unknown reviewer exits 2" "2" "$?"
bash "$SCRIPT" --reviewer r-codex --target spec --doc-file /no/such --rubric-dir "$RUBRICS" >/dev/null 2>&1
check "missing doc exits 2" "2" "$?"
run --reviewer r-codex --target spec --rubric-dir /no/such >/dev/null 2>&1
check "missing rubric dir exits 2" "2" "$?"

rm -rf "$REPO"
echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
