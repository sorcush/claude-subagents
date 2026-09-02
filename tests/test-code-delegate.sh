#!/usr/bin/env bash
# Unit tests for scripts/code-delegate.sh across all three harnesses.
# Run: bash tests/test-code-delegate.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/code-delegate.sh"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export CSC_CURSOR_BIN="$HERE/mock-cursor-agent"
export CSC_CODEX_BIN="$HERE/mock-codex"
export CSC_CLAUDE_BIN="$HERE/mock-claude"

cat > "$TMP/coders.json" <<'EOF'
{"coders":[
  {"key":"c-cursor","label":"Cursor","harness":"cursor","model":"m-cursor"},
  {"key":"c-codex","label":"Codex","harness":"codex","model":"m-codex"},
  {"key":"c-claude","label":"Claude","harness":"claude","model":"m-claude"}]}
EOF
export CSC_CODERS_JSON="$TMP/coders.json"

# The worktree the coder is supposed to work in.
WT=$(cd "$(mktemp -d)" && pwd -P)
(cd "$WT" && git init -q && git commit -q --allow-empty -m init)
task="$TMP/task.md"; echo "Do the thing." > "$task"

run() { bash "$SCRIPT" --task-file "$task" --cwd "$WT" "$@"; }

# --- happy path on every harness ---
for k in c-cursor c-codex c-claude; do
  out=$(MOCK_RESULT="did it" run --coder "$k" --verify-cmd "true" 2>/dev/null)
  check "$k reports DONE"        "DONE"   "$(echo "$out" | jq -r '.status')"
  check "$k echoes coder key"    "$k"     "$(echo "$out" | jq -r '.coder')"
  check "$k verified true"       "true"   "$(echo "$out" | jq -r '.verified')"
  check "$k zero attempts"       "0"      "$(echo "$out" | jq -r '.attempts')"
  check "$k commit_id is empty"   ""       "$(echo "$out" | jq -r '.commit_id')"
  check "$k emits one JSON line" "1"      "$(echo "$out" | wc -l | tr -d ' ')"
done

out=$(MOCK_SESSION="sess-xyz-991" run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "reports the coder's real session id" "sess-xyz-991" "$(echo "$out" | jq -r '.session_id')"

# --- no verify command: trusted, but reported as unverified ---
out=$(run --coder c-codex --verify-cmd "" 2>/dev/null)
check "empty verify cmd is DONE"      "DONE"  "$(echo "$out" | jq -r '.status')"
check "empty verify cmd unverified"   "false" "$(echo "$out" | jq -r '.verified')"

# --- the verify command runs in --cwd, NOT in the caller's directory ---
# This is the single most likely porting bug: the old cc-delegate.sh ran
# `eval "$VERIFY_CMD"` wherever it happened to be invoked from.
out=$(cd "$TMP" && bash "$SCRIPT" --task-file "$task" --cwd "$WT" \
      --coder c-codex --verify-cmd "pwd > $TMP/verify-cwd.txt" 2>/dev/null)
check "verify ran in the worktree" "$WT" "$(cat "$TMP/verify-cwd.txt" 2>/dev/null)"

# --- retry loop ---
cnt="$TMP/attempts"; echo 0 > "$cnt"
vc="n=\$(cat $cnt); n=\$((n+1)); echo \$n > $cnt; [ \$n -ge 3 ]"
out=$(run --coder c-codex --verify-cmd "$vc" --max-retries 3 2>/dev/null)
check "retries until verify passes" "DONE" "$(echo "$out" | jq -r '.status')"
check "reports the attempt count"   "2"    "$(echo "$out" | jq -r '.attempts')"

out=$(run --coder c-codex --verify-cmd "false" --max-retries 2 2>/dev/null)
check "exhausted retries is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "exhausted retries counts up"  "2"       "$(echo "$out" | jq -r '.attempts')"
check "verify output is kept"        "1" \
  "$([[ -n "$(echo "$out" | jq -r '.verify_output')" ]] && echo 1 || echo 0)"
# Guards the fallback above: a verify command that DOES print must have its real
# output propagated verbatim. Without this, removing the capture entirely still
# passes, because the fallback alone satisfies a non-emptiness check. Verified:
# with the capture removed, the suite reported 44/44 before this test existed.
out=$(run --coder c-codex --verify-cmd "echo distinctive-marker-8842; false" --max-retries 1 2>/dev/null)
check "real verify output is propagated" "1" \
  "$([[ "$(echo "$out" | jq -r '.verify_output')" == *distinctive-marker-8842* ]] && echo 1 || echo 0)"

run --coder c-codex --verify-cmd "false" --max-retries 1 >/dev/null 2>&1
check "BLOCKED exits 1" "1" "$?"

# --- edit mode is requested, not read-only ---
log="$TMP/args.log"
MOCK_LOG="$log" run --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "codex coder requests write access" "1" "$(grep -c -- '-s workspace-write' "$log")"
check "codex coder gets -C worktree" "1" "$(grep -c -- "-C $WT" "$log")"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --coder c-claude --verify-cmd "true" >/dev/null 2>&1
check "claude coder accepts edits" "1" "$(grep -c -- 'acceptEdits' "$log")"
rm -f "$log"

# --- failures ---
out=$(MOCK_FAIL_CLI=1 run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "tool failure is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"

# A result with no session id means the tool never really ran.
out=$(MOCK_SESSION="" run --coder c-cursor --verify-cmd "true" 2>/dev/null)
check "empty session id is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"

# --- argument validation ---
bash "$SCRIPT" >/dev/null 2>&1
check "no args exits 2" "2" "$?"
bash "$SCRIPT" --task-file "$task" --cwd "$WT" --coder c-codex >/dev/null 2>&1
check "missing --verify-cmd exits 2" "2" "$?"
bash "$SCRIPT" --task-file /no/such --cwd "$WT" --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "missing task file exits 2" "2" "$?"
bash "$SCRIPT" --task-file "$task" --cwd /no/such --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "missing --cwd exits 2" "2" "$?"
bash "$SCRIPT" --task-file "$task" --cwd "$TMP" --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "--cwd outside a git repo exits 2" "2" "$?"
run --coder nosuch --verify-cmd "true" >/dev/null 2>&1
check "unknown coder exits 2" "2" "$?"

for bad in "-1" "" "abc" "3+3"; do
  run --coder c-codex --verify-cmd "true" --max-retries "$bad" >/dev/null 2>&1
  check "--max-retries '$bad' exits 2" "2" "$?"
done

# --- a timeout is reported as such, not as a generic failure ---
out=$(CSC_RUN_TIMEOUT=1 MOCK_SLEEP=5 run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "timeout is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "timeout says so in the output" "1" \
  "$([[ "$(echo "$out" | jq -r '.verify_output')" == *timed\ out* ]] && echo 1 || echo 0)"

# --- a timeout must still report changes the coder left behind, not a
# hardcoded false — a caller trusting changed:false may discard finished work.
echo "left behind" > "$WT/timeout-leftover.txt"
out=$(CSC_RUN_TIMEOUT=1 MOCK_SLEEP=5 run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "timeout with dirty worktree reports changed:true" "true" "$(echo "$out" | jq -r '.changed')"
rm -f "$WT/timeout-leftover.txt"

# --- a timeout must still report the real session id, not an empty one —
# every harness announces its session before doing any work, so a run killed
# mid-work should still be resumable with --session.
for k in c-cursor c-codex c-claude; do
  out=$(CSC_RUN_TIMEOUT=1 MOCK_SLEEP=5 MOCK_SESSION="sess-timeout-$k" \
        run --coder "$k" --verify-cmd "true" 2>/dev/null)
  check "$k timeout reports the real session id" "sess-timeout-$k" \
    "$(echo "$out" | jq -r '.session_id')"
done

rm -rf "$WT"
echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
