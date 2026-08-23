#!/usr/bin/env bash
# code-delegate.sh — delegate ONE task to any coder from the pool, inside a
# worktree, then verify and retry.
# Only the final STATUS JSON goes to stdout; progress and diagnostics to stderr.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pool.sh
source "$SCRIPT_DIR/lib/pool.sh"
# shellcheck source=lib/timeout.sh
source "$SCRIPT_DIR/lib/timeout.sh"
# shellcheck source=lib/harness.sh
source "$SCRIPT_DIR/lib/harness.sh"

usage() {
  echo "usage: code-delegate.sh --coder <key> --task-file <path> --verify-cmd <cmd> --cwd <dir>" >&2
  echo "       [--max-retries N] [--session <id>]" >&2
}

CODER=""; TASK_FILE=""; VERIFY_CMD=""; VERIFY_SET=0; CWD=""; MAX_RETRIES=3; SESSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --coder)       CODER="${2:-}";       shift 2 ;;
    --task-file)   TASK_FILE="${2:-}";   shift 2 ;;
    --verify-cmd)  VERIFY_CMD="${2:-}"; VERIFY_SET=1; shift 2 ;;
    --cwd)         CWD="${2:-}";         shift 2 ;;
    --max-retries)
      [[ $# -ge 2 ]] || { echo "error: --max-retries requires a value" >&2; usage; exit 2; }
      MAX_RETRIES="$2"
      shift 2
      ;;
    --session)     SESSION="${2:-}";     shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$CODER" ]] || { echo "error: --coder is required" >&2; usage; exit 2; }
[[ -n "$TASK_FILE" && -r "$TASK_FILE" ]] \
  || { echo "error: --task-file missing or unreadable" >&2; usage; exit 2; }
[[ "$VERIFY_SET" -eq 1 ]] \
  || { echo "error: --verify-cmd is required (use \"\" for no verification)" >&2; usage; exit 2; }
[[ -n "$CWD" && -d "$CWD" ]] \
  || { echo "error: --cwd missing or not a directory" >&2; usage; exit 2; }
# Unvalidated, a value like "-1" or "3+x" reaches an arithmetic comparison and
# either loops forever or blows up with a syntax error.
[[ "$MAX_RETRIES" =~ ^[0-9]+$ ]] \
  || { echo "error: --max-retries must be a non-negative integer, got '$MAX_RETRIES'" >&2; exit 2; }

# Resolve to an absolute path and require a real git worktree. Everything below
# runs here: the task, every retry, the verify command, and every git call.
CWD="$(cd "$CWD" && pwd -P)"
git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "error: --cwd is not inside a git worktree: $CWD" >&2; exit 2; }

pool_load "$(pool_file_for coder)" coder
pool_get "$CODER"
harness_load "$ENTRY_HARNESS"

ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT
SESSION_ID=""
RESULT=""

emit() {  # emit <status> <session> <attempts> <verified> <changed> <result> <verify_output>
  jq -nc --arg status "$1" --arg coder "$CODER" --arg session "$2" \
         --argjson attempts "$3" --argjson verified "$4" --argjson changed "$5" \
         --arg result "$6" --arg vout "$7" \
    '{status:$status, coder:$coder, session_id:$session, attempts:$attempts,
      verified:$verified, changed:$changed, commit_id:"",
      result:$result, verify_output:$vout}'
}

# Did the coder actually change anything in the worktree?
changed_flag() {
  if [[ -n "$(git -C "$CWD" status --porcelain 2>/dev/null)" ]]; then echo true; else echo false; fi
}

# Run the verify command INSIDE the worktree. The old cc-delegate.sh ran this
# in the caller's directory, which would test the wrong tree entirely.
#
# The status is captured into VERIFY_RC and the output into VERIFY_OUT, rather
# than returned through a command substitution. `out=$(cmd); [[ $? -eq 0 ]]` does
# work in bash, but it breaks the moment anyone inserts a line between the two,
# and misreading a failed verification as a pass is the worst bug this script
# could have.
VERIFY_RC=0
VERIFY_OUT=""
run_verify() {
  local f; f=$(mktemp)
  ( cd "$CWD" && eval "$VERIFY_CMD" ) >"$f" 2>&1
  VERIFY_RC=$?
  VERIFY_OUT="$(cat "$f")"
  rm -f "$f"
  if [[ $VERIFY_RC -ne 0 && -z "$VERIFY_OUT" ]]; then
    VERIFY_OUT="verification command exited with status $VERIFY_RC"
  fi
}

PROMPT="Read the file $TASK_FILE and implement the task it describes. Make all necessary code edits."

HARNESS_TIMED_OUT=0
if ! harness_run "edit" "$ENTRY_MODEL" "$CWD" "$PROMPT" "$SESSION"; then
  emit BLOCKED "$SESSION_ID" 0 false false "" "coder invocation failed: $(cat "$ERR_FILE" 2>/dev/null)"
  exit 1
fi

# No session id means the tool never really ran, whatever else it printed.
if [[ -z "$SESSION_ID" ]]; then
  emit BLOCKED "" 0 false "$(changed_flag)" "$RESULT" "coder returned no session id"
  exit 1
fi

if [[ -z "$VERIFY_CMD" ]]; then
  emit DONE "$SESSION_ID" 0 false "$(changed_flag)" "$RESULT" ""
  exit 0
fi

attempts=0
run_verify
if [[ "$VERIFY_RC" -eq 0 ]]; then
  emit DONE "$SESSION_ID" "$attempts" true "$(changed_flag)" "$RESULT" "$VERIFY_OUT"
  exit 0
fi

while [[ $attempts -lt $MAX_RETRIES ]]; do
  attempts=$((attempts+1))
  fix_prompt="The verification command failed with this output:

$VERIFY_OUT

Fix the code so the verification passes. Make all necessary edits."
  prev_session="$SESSION_ID"
  HARNESS_TIMED_OUT=0
  if ! harness_run "edit" "$ENTRY_MODEL" "$CWD" "$fix_prompt" "$prev_session"; then
    emit BLOCKED "$prev_session" "$attempts" false "$(changed_flag)" "$RESULT" \
      "coder failed during fix attempt $attempts: $(cat "$ERR_FILE" 2>/dev/null)"
    exit 1
  fi
  # A resumed call must come back with a real session id too. harness_run clears
  # it first, so an empty value here means the resume did not really happen.
  if [[ -z "$SESSION_ID" ]]; then
    emit BLOCKED "$prev_session" "$attempts" false "$(changed_flag)" "$RESULT" \
      "coder returned no session id on fix attempt $attempts"
    exit 1
  fi
  run_verify
  if [[ "$VERIFY_RC" -eq 0 ]]; then
    emit DONE "$SESSION_ID" "$attempts" true "$(changed_flag)" "$RESULT" "$VERIFY_OUT"
    exit 0
  fi
done

emit BLOCKED "$SESSION_ID" "$attempts" false "$(changed_flag)" "$RESULT" "$VERIFY_OUT"
exit 1
