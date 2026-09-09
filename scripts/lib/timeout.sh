#!/usr/bin/env bash
# timeout.sh (library) — bounded execution with process-group cleanup.
# Source this; do not execute it.
#
# macOS has no `timeout` program by default, so this is hand-rolled. It must
# kill the whole process GROUP: killing only the direct child would leave the
# underlying CLI running, and an edit-capable tool could keep changing files
# after the caller has already reported BLOCKED.

TIMEOUT_HIT=0
RUN_GROUP_STOPPED=0
RUN_GROUP_LINGERED=0
RUN_GROUP_ID=""

TIMEOUT_EXIT=124   # same convention as GNU `timeout`
LINGERING_EXIT=125
UNCONTAINED_EXIT=126

timeout_group_has_live_members() {  # <process-group-id>
  local pgid="$1" rows
  kill -0 -"$pgid" 2>/dev/null || return 1
  rows="$(ps -axo pgid=,stat= 2>/dev/null)" || return 0
  awk -v wanted="$pgid" '
    $1 == wanted && $2 !~ /^Z/ { found=1 }
    END { exit(found ? 0 : 1) }
  ' <<<"$rows"
}

timeout_stop_group() {  # <process-group-id>
  local pgid="$1" deadline
  timeout_group_has_live_members "$pgid" || return 0
  kill -TERM -"$pgid" 2>/dev/null
  deadline=$((SECONDS + 5))
  while timeout_group_has_live_members "$pgid" && [[ $SECONDS -lt $deadline ]]; do
    sleep 0.1
  done
  if timeout_group_has_live_members "$pgid"; then
    kill -KILL -"$pgid" 2>/dev/null
  fi
  deadline=$((SECONDS + 5))
  while timeout_group_has_live_members "$pgid" && [[ $SECONDS -lt $deadline ]]; do
    sleep 0.1
  done
  ! timeout_group_has_live_members "$pgid"
}

# run_with_timeout <seconds> <command...>
# Returns the command's exit status, or 124 if it had to be killed.
# Also sets TIMEOUT_HIT, for callers that are not behind a subshell.
run_with_timeout() {
  local secs="$1"
  shift
  run_with_timeout_in "$secs" "$PWD" "$@"
}

# run_with_timeout_in <seconds> <directory> <command...>
# Like run_with_timeout, but changes directory in the child so the caller keeps
# TIMEOUT_HIT and the process-group state.
run_with_timeout_in() {
  local secs="$1" dir="$2"
  shift 2
  local flag pid watcher rc
  local old_int old_term monitor_was_on=0
  [[ -d "$dir" ]] || return 2
  flag="$(mktemp)"
  TIMEOUT_HIT=0
  RUN_GROUP_STOPPED=0
  RUN_GROUP_LINGERED=0
  RUN_GROUP_ID=""

  # Save the caller's traps so they can be restored. `trap -` would discard
  # them, silently disarming whatever the calling script had installed.
  old_int="$(trap -p INT)"
  old_term="$(trap -p TERM)"

  # `set -m` gives the background job its own process group, so a negative pid
  # in `kill` reaches the command and everything it spawned. Without this, a
  # timed-out CLI would keep running and could still be editing files.
  [[ $- == *m* ]] && monitor_was_on=1
  set -m
  ( cd "$dir" && exec "$@" ) &
  pid=$!
  RUN_GROUP_ID="$pid"
  if [[ -n "${RUN_GROUP_RECORD_FILE:-}" ]]; then
    printf '%s\n' "$RUN_GROUP_ID" > "$RUN_GROUP_RECORD_FILE"
  fi
  [[ "$monitor_was_on" -eq 1 ]] || set +m

  {
    sleep "$secs"
    # Write the flag BEFORE signalling. If we signalled first, the command
    # could be reaped and its pid reused by an unrelated process before we
    # recorded anything, and we would have no way to tell the two apart.
    # `kill -0` alone is not enough: it also succeeds for a ZOMBIE, a process that
    # has already exited and is only waiting to be reaped. Without the extra check,
    # a command that finished at the exact instant the deadline fell would be
    # recorded as a timeout and its real exit status thrown away.
    if kill -0 "$pid" 2>/dev/null \
       && ! ps -p "$pid" -o stat= 2>/dev/null | grep -q '^[[:space:]]*Z'; then
      echo 1 > "$flag"
      kill -TERM -"$pid" 2>/dev/null
    fi
  } &
  watcher=$!

  # Deliberately untested: triggering this trap needs a signal delivered mid-wait.
  # While the child runs, an interrupt should take the whole group down rather
  # than orphaning it.
  trap 'kill -KILL -'"$pid"' 2>/dev/null; kill '"$watcher"' 2>/dev/null' INT TERM

  wait "$pid"; rc=$?

  # Stop the watcher first, so it cannot signal a pid that has now been reaped.
  kill "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null

  # Restore exactly what the caller had, including "no trap at all".
  if [[ -n "$old_int" ]];  then eval "$old_int";  else trap - INT;  fi
  if [[ -n "$old_term" ]]; then eval "$old_term"; else trap - TERM; fi

  [[ -s "$flag" ]] && TIMEOUT_HIT=1

  if timeout_group_has_live_members "$RUN_GROUP_ID"; then
    [[ "$TIMEOUT_HIT" -eq 0 ]] && RUN_GROUP_LINGERED=1
    timeout_stop_group "$RUN_GROUP_ID" || true
  fi

  if [[ "${TIMEOUT_TEST_FORCE_UNCONTAINED:-0}" == "1" ]] \
     || timeout_group_has_live_members "$RUN_GROUP_ID"; then
    RUN_GROUP_STOPPED=0
    rm -f "$flag"
    return "$UNCONTAINED_EXIT"
  fi

  RUN_GROUP_STOPPED=1
  rm -f "$flag"
  [[ "$TIMEOUT_HIT" -eq 1 ]] && return "$TIMEOUT_EXIT"
  [[ "$RUN_GROUP_LINGERED" -eq 1 ]] && return "$LINGERING_EXIT"
  return "$rc"
}
