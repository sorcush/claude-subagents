#!/usr/bin/env bash
# Unit tests for scripts/lib/timeout.sh. Run: bash tests/test-timeout.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib/timeout.sh
source "$HERE/../scripts/lib/timeout.sh"

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
TEST_SHELL_PID=$BASHPID
cleanup() {
  [[ $BASHPID -eq $TEST_SHELL_PID ]] && rm -rf "$TMP"
}
trap cleanup EXIT

# --- a fast command is untouched ---
run_with_timeout 10 true
check "fast command returns its own status" "0" "$?"
check "fast command does not set TIMEOUT_HIT" "0" "$TIMEOUT_HIT"

run_with_timeout 10 false
check "failing command returns its own status" "1" "$?"
check "failing command does not set TIMEOUT_HIT" "0" "$TIMEOUT_HIT"

# --- a slow command is killed ---
start=$SECONDS
run_with_timeout 1 sleep 30
rc=$?
elapsed=$(( SECONDS - start ))
check "slow command returns 124"          "124" "$rc"
check "slow command sets TIMEOUT_HIT"     "1"   "$TIMEOUT_HIT"
check "returns promptly, not after 30s"   "1"   "$([[ $elapsed -lt 15 ]] && echo 1 || echo 0)"

# 124 must survive a subshell, because harness_run wraps the call in ( cd ... ).
# A variable would not survive, which is exactly why the status carries the signal.
( cd / && run_with_timeout 1 sleep 30 )
check "124 crosses a subshell boundary" "124" "$?"

# A command that genuinely exits 124 on its own must not be mistaken for a timeout,
# so the helper reports TIMEOUT_HIT=0 there and the caller can tell them apart.
run_with_timeout 10 bash -c "exit 124"
check "a real 124 is passed through" "124" "$?"
check "a real 124 is not a timeout"  "0"   "$TIMEOUT_HIT"

# --- the caller's own traps are restored, not wiped ---
trap 'echo CALLER_TRAP' INT
run_with_timeout 10 true
check "caller INT trap survives" "1" \
  "$([[ "$(trap -p INT)" == *CALLER_TRAP* ]] && echo 1 || echo 0)"
trap - INT

# --- the whole process GROUP dies, not just the direct child ---
# The parent script spawns a grandchild and records its pid, then sleeps.
# After the timeout, that grandchild must be gone: killing only the parent
# would leave a tool still running and possibly still editing files.
cat > "$TMP/spawner.sh" <<'SPAWN'
#!/usr/bin/env bash
sleep 300 &
echo $! > "$1"
sleep 300
SPAWN
chmod +x "$TMP/spawner.sh"

run_with_timeout 3 "$TMP/spawner.sh" "$TMP/grandchild.pid"
gc=$(cat "$TMP/grandchild.pid" 2>/dev/null || echo "")
check "grandchild pid was recorded" "1" "$([[ -n "$gc" ]] && echo 1 || echo 0)"

# Without this guard, an empty $gc makes `ps -p ""` nondeterministic on macOS and the
# check below becomes a coin flip instead of a failure.
if [[ -z "$gc" ]]; then
  check "grandchild is really gone, not just unsignalable" "1" "0"
else
  gone=0
  for _ in $(seq 1 50); do
    if ! ps -p "$gc" -o stat= 2>/dev/null | grep -qv '^[[:space:]]*Z'; then gone=1; break; fi
    sleep 0.2
  done
  check "grandchild is really gone, not just unsignalable" "1" "$gone"
fi

# --- bounded capture preserves normal output, errors, and child status ---
CAPTURE="$HERE/../scripts/lib/run-captured.py"
stdout_file="$TMP/captured-normal.stdout"
stderr_file="$TMP/captured-normal.stderr"
printf 'existing-error\n' > "$stderr_file"
python3 "$CAPTURE" --timeout-seconds 10 --max-stdout-bytes 65536 \
  --stdout-file "$stdout_file" --stderr-file "$stderr_file" --cwd "$TMP" -- \
  /bin/sh -c "printf normal-output; printf 'new-error\\n' >&2; exit 7"
check "bounded capture returns child status" "7" "$?"
check "bounded capture writes stdout" "normal-output" "$(<"$stdout_file")"
check "bounded capture appends stderr" $'existing-error\nnew-error' "$(<"$stderr_file")"

python3 "$CAPTURE" --timeout-seconds 1 --max-stdout-bytes 65536 \
  --stdout-file "$TMP/captured-timeout.stdout" --stderr-file "$TMP/captured-timeout.stderr" \
  --cwd "$TMP" -- sleep 30
check "bounded capture returns 124 on timeout" "124" "$?"

# --- bounded capture removes overflow and kills the whole new session ---
cat > "$TMP/capture-spawner.sh" <<'SPAWN'
#!/usr/bin/env bash
set -u
echo "$$" > "$1"
sleep 300 &
echo "$!" > "$2"
printf -v chunk '%4096s' ''
chunk=${chunk// /x}
for ((i=0; i<32; i++)); do printf '%s' "$chunk"; done
wait
SPAWN
chmod +x "$TMP/capture-spawner.sh"

stdout_file="$TMP/captured.stdout"
stderr_file="$TMP/captured.stderr"
python3 "$CAPTURE" --timeout-seconds 30 --max-stdout-bytes 65536 \
  --stdout-file "$stdout_file" --stderr-file "$stderr_file" --cwd "$TMP" -- \
  "$TMP/capture-spawner.sh" "$TMP/captured-child.pid" "$TMP/captured-grandchild.pid"
capture_rc=$?
check "bounded capture returns 125 on overflow" "125" "$capture_rc"
check "bounded capture removes partial stdout" "0" "$([[ -e "$stdout_file" ]] && echo 1 || echo 0)"

capture_child=$(cat "$TMP/captured-child.pid" 2>/dev/null || echo "")
capture_grandchild=$(cat "$TMP/captured-grandchild.pid" 2>/dev/null || echo "")
check "bounded capture child pid was recorded" "1" "$([[ -n "$capture_child" ]] && echo 1 || echo 0)"
check "bounded capture grandchild pid was recorded" "1" "$([[ -n "$capture_grandchild" ]] && echo 1 || echo 0)"

process_gone() {
  local pid="$1" gone=0
  [[ -z "$pid" ]] && { echo 0; return; }
  for _ in $(seq 1 50); do
    if ! ps -p "$pid" -o stat= 2>/dev/null | grep -qv '^[[:space:]]*Z'; then
      gone=1
      break
    fi
    sleep 0.2
  done
  echo "$gone"
}
check "bounded capture kills direct child" "1" "$(process_gone "$capture_child")"
check "bounded capture kills grandchild" "1" "$(process_gone "$capture_grandchild")"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
