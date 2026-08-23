#!/usr/bin/env bash
# timeout.sh (library) — bounded execution with process-group cleanup.
# Source this; do not execute it.
#
# macOS has no `timeout` program by default, so this is hand-rolled. It must
# kill the whole process GROUP: killing only the direct child would leave the
# underlying CLI running, and an edit-capable tool could keep changing files
# after the caller has already reported BLOCKED.

TIMEOUT_HIT=0

TIMEOUT_EXIT=124   # same convention as GNU `timeout`

# run_with_timeout <seconds> <command...>
# Returns the command's exit status, or 124 if it had to be killed.
# Also sets TIMEOUT_HIT, for callers that are not behind a subshell.
run_with_timeout() {
  local secs="$1"; shift
  local flag pid watcher rc
  local old_int old_term
  flag="$(mktemp)"
  TIMEOUT_HIT=0

  # Save the caller's traps so they can be restored. `trap -` would discard
  # them, silently disarming whatever the calling script had installed.
  old_int="$(trap -p INT)"
  old_term="$(trap -p TERM)"

  # `set -m` gives the background job its own process group, so a negative pid
  # in `kill` reaches the command and everything it spawned. Without this, a
  # timed-out CLI would keep running and could still be editing files.
  set -m
  "$@" &
  pid=$!
  set +m

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
      sleep 5
      # Re-check: the TERM may already have worked, and by now this pid could
      # belong to something else entirely.
      if [[ -s "$flag" ]] && kill -0 "$pid" 2>/dev/null; then
        # Accepted limitation: signalling by PID cannot rule out the kernel having
        # recycled that PID between the child being reaped and this line. Closing
        # that needs pidfd, which is not portable to macOS. The window is
        # microseconds and the guard above makes it narrower still.
        kill -KILL -"$pid" 2>/dev/null
      fi
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

  if [[ -s "$flag" ]]; then
    TIMEOUT_HIT=1
    rm -f "$flag"
    return "$TIMEOUT_EXIT"
  fi
  rm -f "$flag"
  return "$rc"
}
