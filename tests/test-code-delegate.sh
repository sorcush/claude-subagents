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

# The coder must run in a linked feature worktree, never the primary checkout.
MAIN="$TMP/main"
WT="$TMP/task-work"
WT_MAIN="$TMP/main-work"
WT_MASTER="$TMP/master-work"
git init -q -b main "$MAIN"
git -C "$MAIN" config user.email test@example.com
git -C "$MAIN" config user.name Test
echo base > "$MAIN/base.txt"
git -C "$MAIN" add base.txt
git -C "$MAIN" commit -q -m init
git -C "$MAIN" switch -q -c setup
git -C "$MAIN" branch master main
git -C "$MAIN" worktree add -q -b feature/test "$WT" main
git -C "$MAIN" worktree add -q "$WT_MAIN" main
git -C "$MAIN" worktree add -q "$WT_MASTER" master
WT=$(cd "$WT" && pwd -P)
task="$TMP/task.md"; echo "Do the thing." > "$task"

run() {
  bash "$SCRIPT" --task-file "$task" --cwd "$WT" \
    --commit-message "test: delegated task" "$@"
}

clear_lifecycle_at() {
  local worktree="$1" private_git
  private_git=$(git -C "$worktree" rev-parse --absolute-git-dir)
  rm -rf "$private_git/claude-subagents-coder.lock" \
    "$private_git/claude-subagents-coder-recovery.lock"
  rm -f "$private_git/claude-subagents-coder-state.json"
  git -C "$worktree" reset --hard -q HEAD
  git -C "$worktree" clean -fdq
}
clear_lifecycle() { clear_lifecycle_at "$WT"; }

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

# --- the lifecycle owns the commit and reports observed state ---
edit_file="$WT/delegated.txt"
out=$(MOCK_EDIT_FILE="$edit_file" MOCK_EDIT_CONTENT="owned change" \
      run --coder c-codex --verify-cmd "test -f '$edit_file'" 2>/dev/null)
edit_commit=$(echo "$out" | jq -r '.commit_id')
check "mock edit reports DONE" "DONE" "$(echo "$out" | jq -r '.status')"
check "mock edit reports changed" "true" "$(echo "$out" | jq -r '.changed')"
check "mock edit reports a real commit" "$edit_commit" "$(git -C "$WT" rev-parse HEAD)"
check "mock edit reports changed path" "delegated.txt" "$(echo "$out" | jq -r '.files_changed[0]')"
check "mock edit leaves worktree clean" "true" "$(echo "$out" | jq -r '.worktree_clean')"
check "mock edit reports writer stopped" "true" "$(echo "$out" | jq -r '.writer_stopped')"
check "mock edit commit exists" "1" "$(git -C "$WT" cat-file -e "$edit_commit^{commit}" 2>/dev/null && echo 1 || echo 0)"
check "mock edit worktree is actually clean" "" "$(git -C "$WT" status --porcelain)"

before_no_change=$(git -C "$WT" rev-parse HEAD)
out=$(run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "no edit reports unchanged" "false" "$(echo "$out" | jq -r '.changed')"
check "no edit has no commit id" "" "$(echo "$out" | jq -r '.commit_id')"
check "no edit creates no commit" "$before_no_change" "$(git -C "$WT" rev-parse HEAD)"
check "result contains every required field" "0" "$(echo "$out" | jq '
  ["status","coder","session_id","lifecycle_id","attempts","verification_mode",
   "verification","verified","changed","commit_id","files_changed","worktree_clean",
   "writer_stopped","result","diagnostic"] - keys | length')"

out=$(MOCK_SESSION="sess-xyz-991" run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "reports the coder's real session id" "sess-xyz-991" "$(echo "$out" | jq -r '.session_id')"

# --- no verify command: trusted, but reported as unverified ---
out=$(run --coder c-codex --verify-cmd "" 2>/dev/null)
check "empty verify cmd is DONE"      "DONE"  "$(echo "$out" | jq -r '.status')"
check "empty verify cmd unverified"   "false" "$(echo "$out" | jq -r '.verified')"

# --- multiple verification commands run in order and rerun as one pass ---
verify_log="$TMP/verify-order"
first="echo first >> '$verify_log'"
second="echo second >> '$verify_log'"
out=$(run --coder c-codex --verify-cmd "$first" --verify-cmd "$second" 2>/dev/null)
check "verification commands preserve order" $'first\nsecond' "$(cat "$verify_log")"
check "verification result has two entries" "2" "$(echo "$out" | jq '.verification|length')"

rm -f "$verify_log"
verify_count="$TMP/verify-count"
echo 0 > "$verify_count"
second_retry="echo second >> '$verify_log'; n=\$(cat '$verify_count'); n=\$((n+1)); echo \$n > '$verify_count'; [ \$n -ge 2 ]"
out=$(run --coder c-codex --verify-cmd "$first" --verify-cmd "$second_retry" --max-retries 1 2>/dev/null)
check "retry reruns the complete verification list" $'first\nsecond\nfirst\nsecond' "$(cat "$verify_log")"
check "retry result keeps the successful full pass" "2" "$(echo "$out" | jq '.verification|length')"

# --- the verify command runs in --cwd, NOT in the caller's directory ---
# This is the single most likely porting bug: the old cc-delegate.sh ran
# `eval "$VERIFY_CMD"` wherever it happened to be invoked from.
out=$(cd "$TMP" && bash "$SCRIPT" --task-file "$task" --cwd "$WT" \
      --commit-message "test: cwd" --coder c-codex \
      --verify-cmd "pwd > $TMP/verify-cwd.txt" 2>/dev/null)
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
clear_lifecycle
check "coder success claim cannot override failed verification" "BLOCKED" \
  "$(MOCK_RESULT='DONE, everything passed' run --coder c-codex --verify-cmd false --max-retries 0 2>/dev/null | jq -r '.status')"
clear_lifecycle
# Guards the fallback above: a verify command that DOES print must have its real
# output propagated verbatim. Without this, removing the capture entirely still
# passes, because the fallback alone satisfies a non-emptiness check. Verified:
# with the capture removed, the suite reported 44/44 before this test existed.
out=$(run --coder c-codex --verify-cmd "echo distinctive-marker-8842; false" --max-retries 1 2>/dev/null)
check "real verify output is propagated" "1" \
  "$([[ "$(echo "$out" | jq -r '.verify_output')" == *distinctive-marker-8842* ]] && echo 1 || echo 0)"
clear_lifecycle

run --coder c-codex --verify-cmd "false" --max-retries 1 >/dev/null 2>&1
check "BLOCKED exits 1" "1" "$?"
clear_lifecycle

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
clear_lifecycle

# A result with no session id cannot commit even when the tool edited a file.
before_missing_session=$(git -C "$WT" rev-parse HEAD)
out=$(MOCK_SESSION="" MOCK_EDIT_FILE="$WT/no-session.txt" \
      run --coder c-cursor --verify-cmd "true" 2>/dev/null)
check "empty session id is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "empty session id creates no commit" "$before_missing_session" "$(git -C "$WT" rev-parse HEAD)"
clear_lifecycle

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
run --coder c-codex --verify-cmd true --commit-message $'bad\nmessage' >/dev/null 2>&1
check "multiline commit message exits 2" "2" "$?"
bash "$SCRIPT" recover --cwd "$WT" --lifecycle-id 1-2-3 --force >/dev/null 2>&1
check "recovery rejects unknown options" "2" "$?"
run --coder nosuch --verify-cmd "true" >/dev/null 2>&1
check "unknown coder exits 2" "2" "$?"

primary_log="$TMP/primary-coder.log"
MOCK_LOG="$primary_log" bash "$SCRIPT" --task-file "$task" --cwd "$MAIN" \
  --commit-message "test: primary" --coder c-codex --verify-cmd true >/dev/null 2>&1
check "primary checkout is rejected" "2" "$?"
check "primary rejection does not invoke coder" "0" "$([[ ! -s "$primary_log" ]] && echo 0 || echo 1)"
MOCK_LOG="$primary_log" bash "$SCRIPT" --task-file "$task" --cwd "$WT_MAIN" \
  --commit-message "test: main" --coder c-codex --verify-cmd true >/dev/null 2>&1
check "linked main branch is rejected" "2" "$?"
MOCK_LOG="$primary_log" bash "$SCRIPT" --task-file "$task" --cwd "$WT_MASTER" \
  --commit-message "test: master" --coder c-codex --verify-cmd true >/dev/null 2>&1
check "linked master branch is rejected" "2" "$?"

validation_log="$TMP/validation-coder.log"
MOCK_LOG="$validation_log" run --coder c-codex --verify-cmd "" --verify-cmd "true" >/dev/null 2>&1
check "empty verification cannot be mixed with commands" "2" "$?"
check "mixed empty verification is rejected before coder invocation" "0" \
  "$([[ ! -s "$validation_log" ]] && echo 0 || echo 1)"

rm -f "$validation_log"
MOCK_LOG="$validation_log" run --coder c-codex --verify-cmd "server: npm test" >/dev/null 2>&1
check "human label is rejected before coder invocation" "2" "$?"
check "human label does not invoke the coder" "0" \
  "$([[ ! -s "$validation_log" ]] && echo 0 || echo 1)"

rm -f "$validation_log"
MOCK_LOG="$validation_log" run --coder c-codex --verify-cmd "server:" >/dev/null 2>&1
check "bare human label is rejected before coder invocation" "2" "$?"
check "bare human label does not invoke the coder" "0" \
  "$([[ ! -s "$validation_log" ]] && echo 0 || echo 1)"

rm -f "$validation_log"
MOCK_LOG="$validation_log" run --coder c-codex --verify-cmd "if then" >/dev/null 2>&1
check "invalid shell syntax is rejected" "2" "$?"
check "invalid shell syntax does not invoke the coder" "0" \
  "$([[ ! -s "$validation_log" ]] && echo 0 || echo 1)"

for bad in "-1" "" "abc" "3+3"; do
  run --coder c-codex --verify-cmd "true" --max-retries "$bad" >/dev/null 2>&1
  check "--max-retries '$bad' exits 2" "2" "$?"
done

system_bash_major=$(/bin/bash -c 'printf %s "${BASH_VERSINFO[0]:-0}"')
if [[ "$system_bash_major" -lt 4 ]]; then
  /bin/bash "$SCRIPT" >/dev/null 2>&1
  check "old Bash is rejected before lifecycle state" "2" "$?"
  check "old Bash creates no lifecycle lock" "0" \
    "$([[ -d "$(git -C "$WT" rev-parse --absolute-git-dir)/claude-subagents-coder.lock" ]] && echo 1 || echo 0)"
else
  check "Bash preflight is supported on this host" "1" "1"
fi

# --- a timeout is reported as such, not as a generic failure ---
out=$(CSC_RUN_TIMEOUT=1 MOCK_SLEEP=5 run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "timeout is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "timeout says so in the output" "1" \
  "$([[ "$(echo "$out" | jq -r '.verify_output')" == *timed\ out* ]] && echo 1 || echo 0)"
clear_lifecycle

# --- a timeout must still report changes the coder left behind, not a
# hardcoded false — a caller trusting changed:false may discard finished work.
out=$(CSC_RUN_TIMEOUT=1 MOCK_SLEEP=5 MOCK_EDIT_FILE="$WT/timeout-leftover.txt" \
      run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "timeout with dirty worktree reports changed:true" "true" "$(echo "$out" | jq -r '.changed')"
timeout_lifecycle=$(echo "$out" | jq -r '.lifecycle_id')
timeout_session=$(echo "$out" | jq -r '.session_id')
out=$(run --coder c-codex --verify-cmd true 2>/dev/null)
check "fresh task is refused while recovery state exists" "BLOCKED" "$(echo "$out" | jq -r '.status')"
out=$(run --coder c-codex --verify-cmd true --session "$timeout_session" --lifecycle-id 1-2-3 2>/dev/null)
check "wrong lifecycle id cannot resume timeout work" "BLOCKED" "$(echo "$out" | jq -r '.status')"
out=$(run --coder c-codex --verify-cmd true --session "$timeout_session" \
      --lifecycle-id "$timeout_lifecycle" 2>/dev/null)
check "matching lifecycle resumes timeout work" "DONE" "$(echo "$out" | jq -r '.status')"
check "resumed timeout work is committed" "timeout-leftover.txt" \
  "$(echo "$out" | jq -r '.files_changed[0]')"

# --- a timeout must still report the real session id, not an empty one —
# every harness announces its session before doing any work, so a run killed
# mid-work should still be resumable with --session.
for k in c-cursor c-codex c-claude; do
  out=$(CSC_RUN_TIMEOUT=1 MOCK_SLEEP=5 MOCK_SESSION="sess-timeout-$k" \
        run --coder "$k" --verify-cmd "true" 2>/dev/null)
  check "$k timeout reports the real session id" "sess-timeout-$k" \
    "$(echo "$out" | jq -r '.session_id')"
  clear_lifecycle
done

# --- verification timeout blocks without another coder pass ---
verify_timeout_log="$TMP/verify-timeout-coder.log"
out=$(CSC_VERIFY_TIMEOUT=1 MOCK_LOG="$verify_timeout_log" MOCK_SESSION="sess-verify-timeout" \
      run --coder c-codex --verify-cmd "sleep 5" --max-retries 3 2>/dev/null)
check "verification timeout is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "verification timeout is structured" "true" "$(echo "$out" | jq -r '.verification[-1].timed_out')"
check "verification timeout preserves session" "sess-verify-timeout" "$(echo "$out" | jq -r '.session_id')"
check "verification timeout does not retry coder" "1" \
  "$(grep -o 'ARGS:' "$verify_timeout_log" | wc -l | tr -d ' ')"
clear_lifecycle

# --- concurrent ownership: a second task never starts its coder ---
first_out="$TMP/first-owner.json"
first_log="$TMP/first-owner.log"
second_log="$TMP/second-owner.log"
CSC_RUN_TIMEOUT=2 MOCK_SLEEP=5 MOCK_LOG="$first_log" \
  MOCK_EDIT_FILE="$WT/first-owner.txt" run --coder c-codex --verify-cmd true \
  >"$first_out" 2>/dev/null &
first_pid=$!
private_git=$(git -C "$WT" rev-parse --absolute-git-dir)
for _ in $(seq 1 50); do
  [[ -f "$private_git/claude-subagents-coder.lock/owner.json" ]] && break
  sleep 0.1
done
out=$(MOCK_LOG="$second_log" run --coder c-cursor --verify-cmd true 2>/dev/null)
check "second lifecycle is blocked" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "second lifecycle does not invoke coder" "0" "$([[ ! -s "$second_log" ]] && echo 0 || echo 1)"
check "second lifecycle preserves first edit" "delegated change" "$(cat "$WT/first-owner.txt")"
wait "$first_pid" 2>/dev/null
check "timed out first owner is blocked" "BLOCKED" "$(jq -r '.status' "$first_out")"
clear_lifecycle

# --- an interrupt stops the active writer before releasing ownership ---
WT_SIGNAL="$TMP/signal-work"
git -C "$MAIN" worktree add -q -b feature/signal "$WT_SIGNAL" main
signal_out="$TMP/signal-owner.json"
MOCK_SLEEP=30 bash "$SCRIPT" --task-file "$task" --cwd "$WT_SIGNAL" \
  --commit-message "test: signal" --coder c-codex --verify-cmd true \
  >"$signal_out" 2>/dev/null &
signal_owner=$!
signal_git=$(git -C "$WT_SIGNAL" rev-parse --absolute-git-dir)
for _ in $(seq 1 50); do
  signal_pgid=$(cat "$signal_git/claude-subagents-coder.lock/process_group_id" 2>/dev/null)
  [[ "$signal_pgid" =~ ^[1-9][0-9]*$ ]] && break
  sleep 0.1
done
kill -TERM "$signal_owner" 2>/dev/null
wait "$signal_owner" 2>/dev/null
check "interrupted lifecycle is BLOCKED" "BLOCKED" "$(jq -r '.status' "$signal_out")"
check "interrupted writer group is stopped" "0" \
  "$(kill -0 -"$signal_pgid" 2>/dev/null && echo 1 || echo 0)"
check "interrupted lifecycle releases live lock" "0" \
  "$([[ -d "$signal_git/claude-subagents-coder.lock" ]] && echo 1 || echo 0)"
clear_lifecycle_at "$WT_SIGNAL"

# --- an abrupt controller death leaves an active record until the writer stops ---
WT_CRASH="$TMP/crash-work"
git -C "$MAIN" worktree add -q -b feature/crash "$WT_CRASH" main
crash_out="$TMP/crash-owner.json"
MOCK_SLEEP=30 bash "$SCRIPT" --task-file "$task" --cwd "$WT_CRASH" \
  --commit-message "test: crash" --coder c-codex --verify-cmd true \
  >"$crash_out" 2>/dev/null &
crash_owner=$!
crash_git=$(git -C "$WT_CRASH" rev-parse --absolute-git-dir)
for _ in $(seq 1 50); do
  crash_pgid=$(cat "$crash_git/claude-subagents-coder.lock/process_group_id" 2>/dev/null)
  [[ "$crash_pgid" =~ ^[1-9][0-9]*$ ]] && break
  sleep 0.1
done
crash_lifecycle=$(jq -r '.lifecycle_id' "$crash_git/claude-subagents-coder-state.json")
kill -KILL "$crash_owner" 2>/dev/null
wait "$crash_owner" 2>/dev/null
out=$(bash "$SCRIPT" recover --cwd "$WT_CRASH" --lifecycle-id "$crash_lifecycle" 2>/dev/null)
check "crashed active lifecycle refuses recovery while writer lives" "BLOCKED" \
  "$(echo "$out" | jq -r '.status')"
kill -TERM -"$crash_pgid" 2>/dev/null
for _ in $(seq 1 50); do
  kill -0 -"$crash_pgid" 2>/dev/null || break
  sleep 0.1
done
out=$(bash "$SCRIPT" recover --cwd "$WT_CRASH" --lifecycle-id "$crash_lifecycle" 2>/dev/null)
check "stale active lifecycle can be recovered" "RECOVERED" "$(echo "$out" | jq -r '.status')"
clear_lifecycle_at "$WT_CRASH"

# --- recovery observes the verification process, not the earlier coder process ---
WT_VERIFY_CRASH="$TMP/verify-crash-work"
git -C "$MAIN" worktree add -q -b feature/verify-crash "$WT_VERIFY_CRASH" main
verify_pid_file="$TMP/verify-crash.pid"
verify_out="$TMP/verify-crash.json"
verify_command="printf '%s\\n' \$\$ > '$verify_pid_file'; sleep 30"
bash "$SCRIPT" --task-file "$task" --cwd "$WT_VERIFY_CRASH" \
  --commit-message "test: verification crash" --coder c-codex \
  --verify-cmd "$verify_command" >"$verify_out" 2>/dev/null &
verify_owner=$!
verify_git=$(git -C "$WT_VERIFY_CRASH" rev-parse --absolute-git-dir)
for _ in $(seq 1 100); do
  verify_pgid=$(cat "$verify_git/claude-subagents-coder.lock/process_group_id" 2>/dev/null)
  [[ -s "$verify_pid_file" && "$verify_pgid" =~ ^[1-9][0-9]*$ ]] && break
  sleep 0.1
done
verify_lifecycle=$(jq -r '.lifecycle_id' "$verify_git/claude-subagents-coder-state.json")
kill -KILL "$verify_owner" 2>/dev/null
wait "$verify_owner" 2>/dev/null
out=$(bash "$SCRIPT" recover --cwd "$WT_VERIFY_CRASH" \
  --lifecycle-id "$verify_lifecycle" 2>/dev/null)
check "crashed verification keeps worktree quarantined while verifier lives" "BLOCKED" \
  "$(echo "$out" | jq -r '.status')"
kill -KILL -"$verify_pgid" 2>/dev/null
for _ in $(seq 1 100); do
  kill -0 -"$verify_pgid" 2>/dev/null || break
  sleep 0.1
done
out=$(bash "$SCRIPT" recover --cwd "$WT_VERIFY_CRASH" \
  --lifecycle-id "$verify_lifecycle" 2>/dev/null)
check "stopped crashed verification can be recovered" "RECOVERED" \
  "$(echo "$out" | jq -r '.status')"
clear_lifecycle_at "$WT_VERIFY_CRASH"

# --- a lingering child is killed and the task is blocked ---
linger_pid_file="$TMP/linger.pid"
out=$(MOCK_LINGER_PID_FILE="$linger_pid_file" MOCK_LINGER_SECONDS=300 \
      run --coder c-codex --verify-cmd true 2>/dev/null)
linger_pid=$(cat "$linger_pid_file")
check "lingering writer is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "lingering writer is stopped" "0" "$(kill -0 "$linger_pid" 2>/dev/null && echo 1 || echo 0)"
clear_lifecycle

# --- an unconfirmed shutdown quarantines until explicit recovery ---
out=$(TIMEOUT_TEST_FORCE_UNCONTAINED=1 run --coder c-codex --verify-cmd true 2>/dev/null)
quarantine_lifecycle=$(echo "$out" | jq -r '.lifecycle_id')
quarantine_session=$(echo "$out" | jq -r '.session_id')
check "unconfirmed shutdown is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "unconfirmed shutdown retains quarantine lock" "1" \
  "$([[ -d "$private_git/claude-subagents-coder.lock" ]] && echo 1 || echo 0)"
out=$(bash "$SCRIPT" recover --cwd "$WT" --lifecycle-id "$quarantine_lifecycle" 2>/dev/null)
check "stopped quarantine can be recovered" "RECOVERED" "$(echo "$out" | jq -r '.status')"
out=$(run --coder c-codex --verify-cmd true --session "$quarantine_session" \
      --lifecycle-id "$quarantine_lifecycle" 2>/dev/null)
check "recovered lifecycle can resume" "DONE" "$(echo "$out" | jq -r '.status')"

# --- coder-owned Git changes are preserved and quarantined ---
WT_TAMPER="$TMP/tamper-work"
git -C "$MAIN" worktree add -q -b feature/tamper "$WT_TAMPER" main
tamper_start=$(git -C "$WT_TAMPER" rev-parse HEAD)
tamper_file="$WT_TAMPER/"$'coder\ncommit.txt'
out=$(MOCK_EDIT_FILE="$tamper_file" \
      MOCK_GIT_COMMAND="git add -A && git commit -q -m coder-owned" \
      bash "$SCRIPT" --task-file "$task" --cwd "$WT_TAMPER" \
        --commit-message "test: caller" --coder c-codex --verify-cmd true 2>/dev/null)
tamper_git=$(git -C "$WT_TAMPER" rev-parse --absolute-git-dir)
check "coder-created commit is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "coder-created commit is preserved" "1" \
  "$([[ "$(git -C "$WT_TAMPER" rev-parse HEAD)" != "$tamper_start" ]] && echo 1 || echo 0)"
check "coder-created commit quarantines worktree" "1" \
  "$([[ -d "$tamper_git/claude-subagents-coder.lock" ]] && echo 1 || echo 0)"
tamper_lifecycle=$(echo "$out" | jq -r '.lifecycle_id')
bash "$SCRIPT" recover --cwd "$WT_TAMPER" --lifecycle-id "$tamper_lifecycle" >/dev/null 2>&1
check "coder-created commit cannot be recovered in place" "1" "$?"
git -C "$WT_TAMPER" reset --mixed -q "$tamper_start"
out=$(bash "$SCRIPT" recover --cwd "$WT_TAMPER" --lifecycle-id "$tamper_lifecycle" 2>/dev/null)
check "restored coder commit can be recovered" "RECOVERED" "$(echo "$out" | jq -r '.status')"
check "recovery retains coder commit changes with newline path" "delegated change" "$(cat "$tamper_file")"
clear_lifecycle_at "$WT_TAMPER"

WT_SWITCH="$TMP/switch-work"
git -C "$MAIN" worktree add -q -b feature/switch "$WT_SWITCH" main
out=$(MOCK_GIT_COMMAND="git switch -q -c feature/coder-switched" \
      bash "$SCRIPT" --task-file "$task" --cwd "$WT_SWITCH" \
        --commit-message "test: caller" --coder c-codex --verify-cmd true 2>/dev/null)
check "coder branch switch is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "coder branch switch is preserved" "feature/coder-switched" \
  "$(git -C "$WT_SWITCH" branch --show-current)"
switch_lifecycle=$(echo "$out" | jq -r '.lifecycle_id')
bash "$SCRIPT" recover --cwd "$WT_SWITCH" --lifecycle-id "$switch_lifecycle" >/dev/null 2>&1
check "coder branch switch cannot be recovered in place" "1" "$?"
git -C "$WT_SWITCH" switch -q feature/switch
out=$(bash "$SCRIPT" recover --cwd "$WT_SWITCH" --lifecycle-id "$switch_lifecycle" 2>/dev/null)
check "restored coder branch can be recovered" "RECOVERED" "$(echo "$out" | jq -r '.status')"
clear_lifecycle_at "$WT_SWITCH"

# --- failed and dirty commits preserve work for inspection ---
hooks="$TMP/hooks"
mkdir -p "$hooks"
printf '#!/usr/bin/env bash\nexit 1\n' > "$hooks/pre-commit"
chmod +x "$hooks/pre-commit"
git -C "$WT" config core.hooksPath "$hooks"
before_failed_commit=$(git -C "$WT" rev-parse HEAD)
out=$(MOCK_EDIT_FILE="$WT/commit-failed.txt" run --coder c-codex --verify-cmd true 2>/dev/null)
check "failed commit is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "failed commit preserves file" "delegated change" "$(cat "$WT/commit-failed.txt")"
check "failed commit does not rewrite history" "$before_failed_commit" "$(git -C "$WT" rev-parse HEAD)"
git -C "$WT" config --unset core.hooksPath
clear_lifecycle

# --- a crash during a commit hook keeps the hook under lifecycle ownership ---
WT_HOOK_CRASH="$TMP/hook-crash-work"
git -C "$MAIN" worktree add -q -b feature/hook-crash "$WT_HOOK_CRASH" main
hook_crash_dir="$TMP/hook-crash-hooks"
hook_pid_file="$TMP/hook-crash.pid"
hook_crash_out="$TMP/hook-crash.json"
mkdir -p "$hook_crash_dir"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$$" > "$HOOK_PID_FILE"\nsleep 30\n' \
  > "$hook_crash_dir/pre-commit"
chmod +x "$hook_crash_dir/pre-commit"
git -C "$WT_HOOK_CRASH" config core.hooksPath "$hook_crash_dir"
MOCK_EDIT_FILE="$WT_HOOK_CRASH/hook-crash.txt" HOOK_PID_FILE="$hook_pid_file" \
  bash "$SCRIPT" --task-file "$task" --cwd "$WT_HOOK_CRASH" \
    --commit-message "test: hook crash" --coder c-codex --verify-cmd true \
    >"$hook_crash_out" 2>/dev/null &
hook_owner=$!
hook_crash_git=$(git -C "$WT_HOOK_CRASH" rev-parse --absolute-git-dir)
for _ in $(seq 1 100); do
  hook_pgid=$(cat "$hook_crash_git/claude-subagents-coder.lock/process_group_id" 2>/dev/null)
  [[ -s "$hook_pid_file" && "$hook_pgid" =~ ^[1-9][0-9]*$ ]] && break
  sleep 0.1
done
hook_crash_lifecycle=$(jq -r '.lifecycle_id' \
  "$hook_crash_git/claude-subagents-coder-state.json")
kill -KILL "$hook_owner" 2>/dev/null
wait "$hook_owner" 2>/dev/null
out=$(bash "$SCRIPT" recover --cwd "$WT_HOOK_CRASH" \
  --lifecycle-id "$hook_crash_lifecycle" 2>/dev/null)
check "crashed commit hook keeps worktree quarantined while hook lives" "BLOCKED" \
  "$(echo "$out" | jq -r '.status')"
kill -KILL -"$hook_pgid" 2>/dev/null
for _ in $(seq 1 100); do
  kill -0 -"$hook_pgid" 2>/dev/null || break
  sleep 0.1
done
out=$(bash "$SCRIPT" recover --cwd "$WT_HOOK_CRASH" \
  --lifecycle-id "$hook_crash_lifecycle" 2>/dev/null)
check "stopped crashed commit hook can be recovered" "RECOVERED" \
  "$(echo "$out" | jq -r '.status')"
clear_lifecycle_at "$WT_HOOK_CRASH"

# --- a timed-out post-commit hook keeps the commit as the eventual result ---
WT_POST_TIMEOUT="$TMP/post-timeout-work"
git -C "$MAIN" worktree add -q -b feature/post-timeout "$WT_POST_TIMEOUT" main
post_timeout_hooks="$TMP/post-timeout-hooks"
mkdir -p "$post_timeout_hooks"
printf '#!/usr/bin/env bash\nsleep 30\n' > "$post_timeout_hooks/post-commit"
chmod +x "$post_timeout_hooks/post-commit"
git -C "$WT_POST_TIMEOUT" config core.hooksPath "$post_timeout_hooks"
out=$(CSC_GIT_TIMEOUT=1 MOCK_EDIT_FILE="$WT_POST_TIMEOUT/post-timeout.txt" \
  bash "$SCRIPT" --task-file "$task" --cwd "$WT_POST_TIMEOUT" \
    --commit-message "test: post timeout" --coder c-codex --verify-cmd true 2>/dev/null)
post_timeout_lifecycle=$(echo "$out" | jq -r '.lifecycle_id')
post_timeout_session=$(echo "$out" | jq -r '.session_id')
post_timeout_commit=$(git -C "$WT_POST_TIMEOUT" rev-parse HEAD)
check "timed-out post-commit hook is BLOCKED" "BLOCKED" \
  "$(echo "$out" | jq -r '.status')"
git -C "$WT_POST_TIMEOUT" config --unset core.hooksPath
out=$(bash "$SCRIPT" --task-file "$task" --cwd "$WT_POST_TIMEOUT" \
  --commit-message "test: post timeout resume" --coder c-codex --verify-cmd true \
  --session "$post_timeout_session" --lifecycle-id "$post_timeout_lifecycle" 2>/dev/null)
check "post-commit timeout resume is DONE" "DONE" "$(echo "$out" | jq -r '.status')"
check "post-commit timeout resume reports the existing task commit" \
  "$post_timeout_commit" "$(echo "$out" | jq -r '.commit_id')"
check "post-commit timeout resume reports the task file" "post-timeout.txt" \
  "$(echo "$out" | jq -r '.files_changed[0]')"
clear_lifecycle_at "$WT_POST_TIMEOUT"

rm -f "$hooks/pre-commit"
printf '#!/usr/bin/env bash\nprintf "hook change\\n" > hook-leftover.txt\n' > "$hooks/post-commit"
chmod +x "$hooks/post-commit"
git -C "$WT" config core.hooksPath "$hooks"
out=$(MOCK_EDIT_FILE="$WT/commit-with-hook.txt" run --coder c-codex --verify-cmd true 2>/dev/null)
check "hook leftover is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "hook-created commit is preserved" "1" \
  "$([[ -n "$(echo "$out" | jq -r '.commit_id')" ]] && echo 1 || echo 0)"
check "hook leftover is preserved" "hook change" "$(cat "$WT/hook-leftover.txt")"
hook_lifecycle=$(echo "$out" | jq -r '.lifecycle_id')
hook_session=$(echo "$out" | jq -r '.session_id')
git -C "$WT" config --unset core.hooksPath
out=$(run --coder c-codex --verify-cmd true --session "$hook_session" \
      --lifecycle-id "$hook_lifecycle" 2>/dev/null)
check "hook-leftover lifecycle can resume" "DONE" "$(echo "$out" | jq -r '.status')"
check "resumed hook lifecycle still reports changes" "true" \
  "$(echo "$out" | jq -r '.changed')"
check "resumed hook lifecycle reports its final commit" "1" \
  "$([[ -n "$(echo "$out" | jq -r '.commit_id')" ]] && echo 1 || echo 0)"
check "resumed hook lifecycle includes the original task file" "1" \
  "$(echo "$out" | jq 'any(.files_changed[]; . == "commit-with-hook.txt") | if . then 1 else 0 end')"
check "resumed hook lifecycle includes the hook leftover" "1" \
  "$(echo "$out" | jq 'any(.files_changed[]; . == "hook-leftover.txt") | if . then 1 else 0 end')"

# --- completion never reports DONE if ownership release fails ---
WT_FINALIZE="$TMP/finalize-work"
git -C "$MAIN" worktree add -q -b feature/finalize "$WT_FINALIZE" main
out=$(MOCK_EDIT_FILE="$WT_FINALIZE/finalize.txt" MOCK_CORRUPT_LOCK=1 \
      bash "$SCRIPT" --task-file "$task" --cwd "$WT_FINALIZE" \
        --commit-message "test: finalize" --coder c-codex --verify-cmd true 2>/dev/null)
finalize_git=$(git -C "$WT_FINALIZE" rev-parse --absolute-git-dir)
check "failed ownership release is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "failed ownership release retains state" "1" \
  "$([[ -f "$finalize_git/claude-subagents-coder-state.json" ]] && echo 1 || echo 0)"
check "failed ownership release retains lock" "1" \
  "$([[ -d "$finalize_git/claude-subagents-coder.lock" ]] && echo 1 || echo 0)"
clear_lifecycle_at "$WT_FINALIZE"

rm -rf "$WT"
echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
