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

# --- optional verification-home isolation ---
# The unset case must preserve legacy inherited-environment behavior. The set
# case must expose only the minimal explicitly supplied environment.
caller_home="$TMP/caller-home"
verify_home="$TMP/verify-home"
provided_tmpdir="$TMP/provided-tmpdir"
mkdir -p "$caller_home" "$verify_home" "$provided_tmpdir"
chmod 700 "$verify_home"
printf 'caller credential\n' > "$caller_home/credential.txt"
provided_path="/csc-isolation-bin:$PATH"
provided_lang="C"
provided_user="csc-isolation-user"
provided_shell="$BASH"
unset_snapshot="$TMP/verify-env-unset.txt"
set_snapshot="$TMP/verify-env-set.txt"

# Capture values inside the verification shell, not from the delegate process.
capture_env_cmd() {
  local destination="$1"
  printf 'printf "HOME=%%s\\nAZURE_OPENAI_API_KEY=%%s\\nPATH=%%s\\nTMPDIR=%%s\\nLANG=%%s\\nUSER=%%s\\nSHELL=%%s\\nCREDENTIAL=%%s\\n" "$HOME" "${AZURE_OPENAI_API_KEY+present}" "$PATH" "$TMPDIR" "$LANG" "$USER" "$SHELL" "$(if [[ -f "$HOME/credential.txt" ]]; then printf present; else printf absent; fi)" > %q' "$destination"
}

env_value() { grep "^$1=" "$2" | cut -d= -f2-; }

out=$(unset CSC_VERIFY_HOME; HOME="$caller_home" AZURE_OPENAI_API_KEY="caller-secret" \
      PATH="$provided_path" TMPDIR="$provided_tmpdir" LANG="$provided_lang" \
      USER="$provided_user" SHELL="$provided_shell" \
      run --coder c-codex --verify-cmd "$(capture_env_cmd "$unset_snapshot")" 2>/dev/null)
check "unset verify home preserves caller HOME" "$caller_home" "$(env_value HOME "$unset_snapshot")"
check "unset verify home can access caller credential" "present" "$(env_value CREDENTIAL "$unset_snapshot")"

out=$(CSC_VERIFY_HOME="$verify_home" HOME="$caller_home" AZURE_OPENAI_API_KEY="caller-secret" \
      PATH="$provided_path" TMPDIR="$provided_tmpdir" LANG="$provided_lang" \
      USER="$provided_user" SHELL="$provided_shell" \
      run --coder c-codex --verify-cmd "$(capture_env_cmd "$set_snapshot")" 2>/dev/null)
check "isolated verify home is CSC_VERIFY_HOME" "$verify_home" "$(env_value HOME "$set_snapshot")"
check "isolated verify omits Azure OpenAI key" "" "$(env_value AZURE_OPENAI_API_KEY "$set_snapshot")"
check "isolated verify retains supplied PATH" "$provided_path" "$(env_value PATH "$set_snapshot")"
check "isolated verify retains supplied TMPDIR" "$provided_tmpdir" "$(env_value TMPDIR "$set_snapshot")"
check "isolated verify retains supplied LANG" "$provided_lang" "$(env_value LANG "$set_snapshot")"
check "isolated verify retains supplied USER" "$provided_user" "$(env_value USER "$set_snapshot")"
check "isolated verify supplies Bash as SHELL" "$provided_shell" "$(env_value SHELL "$set_snapshot")"
check "isolated verify cannot access caller credential" "absent" "$(env_value CREDENTIAL "$set_snapshot")"

out=$(CSC_VERIFY_HOME="relative-home" run --coder c-codex --verify-cmd "true" --max-retries 0 2>/dev/null)
check "relative CSC_VERIFY_HOME is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "invalid CSC_VERIFY_HOME reports its cause" "invalid CSC_VERIFY_HOME" \
  "$(echo "$out" | jq -r '.verify_output')"

out=$(CSC_VERIFY_HOME="$TMP/missing-verify-home" run --coder c-codex --verify-cmd "true" --max-retries 0 2>/dev/null)
check "missing CSC_VERIFY_HOME is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "missing CSC_VERIFY_HOME reports its cause" "invalid CSC_VERIFY_HOME" \
  "$(echo "$out" | jq -r '.verify_output')"

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

rm -rf "$WT"
echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
