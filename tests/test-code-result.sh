#!/usr/bin/env bash
# Contract tests for scripts/validate-code-result.sh.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATOR="$HERE/../scripts/validate-code-result.sh"
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
result_file="$TMP/result.json"

valid_changed='{"status":"DONE","coder":"c-codex","session_id":"sess-1","lifecycle_id":"1-2-3","attempts":0,"verification_mode":"commands","verification":[{"command":"true","exit_code":0,"timed_out":false,"output":""}],"verified":true,"changed":true,"commit_id":"abc123","files_changed":["a.txt"],"worktree_clean":true,"writer_stopped":true,"result":"done","diagnostic":""}'
valid_unchanged='{"status":"DONE","coder":"c-codex","session_id":"sess-1","lifecycle_id":"1-2-3","attempts":0,"verification_mode":"none","verification":[],"verified":false,"changed":false,"commit_id":"","files_changed":[],"worktree_clean":true,"writer_stopped":true,"result":"done","diagnostic":""}'
valid_blocked='{"status":"BLOCKED","coder":"c-codex","session_id":"sess-1","lifecycle_id":"1-2-3","attempts":1,"verification_mode":"commands","verification":[{"command":"false","exit_code":1,"timed_out":false,"output":"failed"}],"verified":false,"changed":true,"commit_id":"","files_changed":["a.txt"],"worktree_clean":false,"writer_stopped":true,"result":"claimed success","diagnostic":"verification failed"}'

validate() {
  printf '%s\n' "$1" > "$result_file"
  bash "$VALIDATOR" --exit-code "$2" --result-file "$result_file" >/dev/null 2>&1
}

validate "$valid_changed" 0
check "valid changed DONE passes" "0" "$?"
validate "$valid_unchanged" 0
check "valid unchanged DONE passes" "0" "$?"
validate "$valid_blocked" 1
check "valid BLOCKED passes" "1" "$?"

validate 'not-json' 0
check "invalid JSON is rejected" "2" "$?"
printf '%s\n%s\n' "$valid_changed" "$valid_changed" > "$result_file"
bash "$VALIDATOR" --exit-code 0 --result-file "$result_file" >/dev/null 2>&1
check "multiple result lines are rejected" "2" "$?"
validate "$(echo "$valid_changed" | jq -c 'del(.diagnostic)')" 0
check "missing field is rejected" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.attempts="zero"')" 0
check "wrong field type is rejected" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.files_changed=[1]')" 0
check "wrong nested field type is rejected" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.verification_mode="unknown"')" 0
check "unknown verification mode is rejected" "2" "$?"
validate "$valid_changed" 1
check "DONE requires exit zero" "2" "$?"
validate "$valid_blocked" 0
check "BLOCKED requires exit one" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.session_id=""')" 0
check "DONE requires a session" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.coder=""')" 0
check "DONE requires a coder" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.lifecycle_id=""')" 0
check "DONE requires a lifecycle id" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.writer_stopped=false')" 0
check "DONE requires stopped writer" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.worktree_clean=false')" 0
check "DONE requires clean worktree" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.verified=false')" 0
check "command verification must pass" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.verification[0].timed_out=true')" 0
check "timed out verification cannot pass" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.verification[0].exit_code=1')" 0
check "failed verification cannot pass" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.commit_id=""')" 0
check "changed DONE requires commit" "2" "$?"
validate "$(echo "$valid_changed" | jq -c '.files_changed=[]')" 0
check "changed DONE requires changed paths" "2" "$?"
validate "$(echo "$valid_unchanged" | jq -c '.commit_id="abc123"')" 0
check "unchanged DONE forbids commit" "2" "$?"
validate "$(echo "$valid_unchanged" | jq -c '.files_changed=["ghost.txt"]')" 0
check "unchanged DONE forbids changed paths" "2" "$?"
validate "$(echo "$valid_unchanged" | jq -c '.verified=true')" 0
check "no-verification mode stays unverified" "2" "$?"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
