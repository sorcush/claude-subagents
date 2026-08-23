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
trap 'rm -rf "$TMP"' EXIT

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

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
