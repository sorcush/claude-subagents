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

# Invalid deadlines must fail before any child or watchdog is launched.
run_with_timeout 0 sleep 5
check "zero timeout is rejected" "2" "$?"
run_with_timeout invalid true
check "non-numeric timeout is rejected" "2" "$?"
oversize_ran="$TMP/oversize-ran"
run_with_timeout 999999999999999999999999999999999999 bash -c ": > '$oversize_ran'"
check "oversized timeout is rejected" "2" "$?"
check "oversized timeout starts no command" "0" "$([[ ! -e "$oversize_ran" ]]; echo $?)"

# --- a fast command is untouched ---
run_with_timeout 10 true
check "fast command returns its own status" "0" "$?"
check "fast command does not set TIMEOUT_HIT" "0" "$TIMEOUT_HIT"

run_with_timeout 10 false
check "failing command returns its own status" "1" "$?"
check "failing command does not set TIMEOUT_HIT" "0" "$TIMEOUT_HIT"
check "finished command reports group stopped" "1" "$RUN_GROUP_STOPPED"

# A fast command must also stop the watchdog's own timer process. Killing only
# the watchdog shell leaves one orphaned sleep alive for the full timeout.
watchdog_before="$(ps -axo pid=,command= 2>/dev/null \
  | awk '$2 ~ /(^|\/)sleep$/ && $3 == "73" {print $1}')"
run_with_timeout 73 true
watchdog_after="$(ps -axo pid=,command= 2>/dev/null \
  | awk '$2 ~ /(^|\/)sleep$/ && $3 == "73" {print $1}')"
new_watchdogs=""
for watchdog_pid in $watchdog_after; do
  if ! grep -qx "$watchdog_pid" <<<"$watchdog_before"; then
    new_watchdogs="${new_watchdogs}${new_watchdogs:+ }$watchdog_pid"
  fi
done
check "fast command leaves no watchdog timer" "" "$new_watchdogs"
for watchdog_pid in $new_watchdogs; do kill "$watchdog_pid" 2>/dev/null || true; done

# --- the caller can select a working directory without a subshell ---
mkdir "$TMP/run-here"
run_with_timeout_in 10 "$TMP/run-here" bash -c 'pwd > observed-pwd'
check "directory-aware run succeeds" "0" "$?"
check "directory-aware run uses requested cwd" "$TMP/run-here" \
  "$(cat "$TMP/run-here/observed-pwd" 2>/dev/null)"

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

# --- a successful direct child must not hide a lingering writer ---
cat > "$TMP/linger.sh" <<'LINGER'
#!/usr/bin/env bash
sleep 300 &
echo $! > "$1"
exit 0
LINGER
chmod +x "$TMP/linger.sh"

run_with_timeout 10 "$TMP/linger.sh" "$TMP/linger.pid"
check "normal exit with a live descendant is blocked" "125" "$?"
check "lingering descendant is recorded" "1" "$RUN_GROUP_LINGERED"
check "lingering process group is stopped" "1" "$RUN_GROUP_STOPPED"

linger=$(cat "$TMP/linger.pid" 2>/dev/null || echo "")
if [[ -z "$linger" ]]; then
  check "lingering child pid was recorded" "1" "0"
else
  gone=0
  for _ in $(seq 1 50); do
    if ! ps -p "$linger" -o stat= 2>/dev/null | grep -qv '^[[:space:]]*Z'; then gone=1; break; fi
    sleep 0.2
  done
  check "lingering child is gone" "1" "$gone"
fi

# Simulate a group that remains observable after cleanup without a shipped
# test-only environment switch.
timeout_group_has_live_members() { return 0; }
timeout_stop_group() { return 1; }
run_with_timeout 10 true
check "unconfirmed process-group shutdown exits 126" "126" "$?"
check "unconfirmed process group is not stopped" "0" "$RUN_GROUP_STOPPED"
source "$HERE/../scripts/lib/timeout.sh"

# If the parent dies between spawning the gated child and opening the gate,
# the child must eventually exit without ever running the requested command.
gate_ready="$TMP/gate-ready"
gate_pgid_file="$TMP/gate-pgid"
gate_marker="$TMP/gate-marker"
gate_command_ran="$TMP/gate-command-ran"
timeout_write_group_marker() {
  if [[ "$1" == pending ]]; then
    printf 'pending\n' > "$RUN_GROUP_RECORD_FILE"
  else
    printf '%s\n' "$RUN_GROUP_ID" > "$gate_pgid_file"
    : > "$gate_ready"
    while :; do :; done
  fi
}
(
  export TMPDIR="$TMP"
  RUN_GROUP_RECORD_FILE="$gate_marker"
  run_with_timeout 60 bash -c ": > '$gate_command_ran'"
) &
gate_parent=$!
for _ in $(seq 1 100); do
  [[ -s "$gate_pgid_file" && -e "$gate_ready" ]] && break
  sleep 0.05
done
gate_pgid="$(cat "$gate_pgid_file" 2>/dev/null)"
kill -KILL "$gate_parent" 2>/dev/null
wait "$gate_parent" 2>/dev/null
gate_stopped=0
for _ in $(seq 1 70); do
  if ! timeout_group_has_live_members "$gate_pgid"; then gate_stopped=1; break; fi
  sleep 0.1
done
check "unopened launch gate exits after parent death" "1" "$gate_stopped"
check "unopened launch gate never runs command" "0" \
  "$([[ -e "$gate_command_ran" ]] && echo 1 || echo 0)"
if [[ "$gate_stopped" -ne 1 ]]; then timeout_stop_group "$gate_pgid" >/dev/null 2>&1 || true; fi
source "$HERE/../scripts/lib/timeout.sh"
unset RUN_GROUP_RECORD_FILE

# The watchdog must force-stop a writer that ignores graceful termination;
# otherwise the caller blocks forever in wait after the timeout fires.
cat > "$TMP/ignore-term.sh" <<'IGNORE_TERM'
#!/usr/bin/env bash
trap '' TERM
while :; do sleep 1; done
IGNORE_TERM
chmod +x "$TMP/ignore-term.sh"
ignore_marker="$TMP/ignore-marker"
ignore_result="$TMP/ignore-result"
(
  RUN_GROUP_RECORD_FILE="$ignore_marker"
  run_with_timeout 1 "$TMP/ignore-term.sh"
  printf '%s\n' "$?" > "$ignore_result"
) &
ignore_owner=$!
ignore_finished=0
for _ in $(seq 1 90); do
  if [[ -s "$ignore_result" ]]; then ignore_finished=1; break; fi
  sleep 0.1
done
check "TERM-ignoring writer is forcibly stopped" "1" "$ignore_finished"
if [[ "$ignore_finished" -eq 1 ]]; then
  check "TERM-ignoring timeout returns 124" "124" "$(cat "$ignore_result")"
else
  ignore_pgid="$(cat "$ignore_marker" 2>/dev/null)"
  [[ "$ignore_pgid" =~ ^[1-9][0-9]*$ ]] && kill -KILL -"$ignore_pgid" 2>/dev/null || true
  kill -KILL "$ignore_owner" 2>/dev/null || true
fi
wait "$ignore_owner" 2>/dev/null

# Once the watchdog has started, killing its owner must make the watchdog stop
# the writer immediately rather than sleep until the original timeout expires.
crash_marker="$TMP/watcher-crash-marker"
(
  RUN_GROUP_RECORD_FILE="$crash_marker"
  run_with_timeout 61 sleep 61
) &
crash_owner=$!
watcher_pgid=""
for _ in $(seq 1 100); do
  crash_writer_pgid="$(cat "$crash_marker" 2>/dev/null)"
  if [[ "$crash_writer_pgid" =~ ^[1-9][0-9]*$ ]]; then
    watcher_pgid="$(ps -axo ppid=,pgid= 2>/dev/null \
      | awk -v owner="$crash_owner" -v writer="$crash_writer_pgid" \
        '$1 == owner && $2 != writer {print $2; exit}')"
    [[ "$watcher_pgid" =~ ^[1-9][0-9]*$ ]] && break
  fi
  sleep 0.05
done
kill -KILL "$crash_owner" 2>/dev/null
wait "$crash_owner" 2>/dev/null
writer_stopped_after_crash=0
watchdog_stopped_after_crash=0
for _ in $(seq 1 90); do
  timeout_group_has_live_members "$crash_writer_pgid" \
    || writer_stopped_after_crash=1
  timeout_group_has_live_members "$watcher_pgid" \
    || watchdog_stopped_after_crash=1
  [[ "$writer_stopped_after_crash" -eq 1 && "$watchdog_stopped_after_crash" -eq 1 ]] && break
  sleep 0.1
done
check "owner death stops the writer group" "1" "$writer_stopped_after_crash"
check "owner death retires the watchdog group" "1" "$watchdog_stopped_after_crash"
if [[ "$writer_stopped_after_crash" -ne 1 ]]; then
  timeout_stop_group "$crash_writer_pgid" >/dev/null 2>&1 || true
fi
if [[ "$watchdog_stopped_after_crash" -ne 1 ]]; then
  timeout_stop_group "$watcher_pgid" >/dev/null 2>&1 || true
fi

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
