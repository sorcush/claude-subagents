#!/usr/bin/env bash
# code-delegate.sh — own one delegated coding lifecycle from launch to commit.
# Only the final result JSON goes to stdout; progress and diagnostics go to stderr.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pool.sh
source "$SCRIPT_DIR/lib/pool.sh"
# shellcheck source=lib/timeout.sh
source "$SCRIPT_DIR/lib/timeout.sh"
# shellcheck source=lib/harness.sh
source "$SCRIPT_DIR/lib/harness.sh"
# shellcheck source=lib/coder-lifecycle.sh
source "$SCRIPT_DIR/lib/coder-lifecycle.sh"

usage() {
  echo "usage: code-delegate.sh --coder <key> --task-file <path> --verify-cmd <cmd> [--verify-cmd <cmd> ...] --cwd <dir> --commit-message <message>" >&2
  echo "       [--max-retries N] [--session <id>] [--lifecycle-id <id>]" >&2
  echo "       code-delegate.sh recover --cwd <dir> --lifecycle-id <id>" >&2
}

input_error() {
  echo "error: $1" >&2
  usage
  exit 2
}

[[ "${BASH_VERSINFO[0]:-0}" -ge 4 ]] \
  || input_error "code-delegate requires Bash 4 or newer"

json_changed_paths() {
  {
    if [[ -n "$LIFECYCLE_START_COMMIT" ]] \
       && git -C "$CWD" cat-file -e "$LIFECYCLE_START_COMMIT^{commit}" 2>/dev/null; then
      git -C "$CWD" diff --name-only -z "$LIFECYCLE_START_COMMIT..HEAD" 2>/dev/null
    fi
    git -C "$CWD" diff --name-only -z 2>/dev/null
    git -C "$CWD" diff --cached --name-only -z 2>/dev/null
    git -C "$CWD" ls-files --others --exclude-standard -z 2>/dev/null
  } | jq -Rs 'split("\u0000") | map(select(length > 0)) | unique'
}

# Recovery is deliberately separate: it never loads a coder or launches a tool.
if [[ "${1:-}" == "recover" ]]; then
  shift
  CWD=""
  REQUESTED_LIFECYCLE_ID=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cwd)
        [[ $# -ge 2 ]] || input_error "--cwd requires a value"
        CWD="$2"; shift 2 ;;
      --lifecycle-id)
        [[ $# -ge 2 ]] || input_error "--lifecycle-id requires a value"
        REQUESTED_LIFECYCLE_ID="$2"; shift 2 ;;
      *) input_error "unknown recovery argument: $1" ;;
    esac
  done
  [[ -n "$CWD" ]] || input_error "--cwd is required"
  [[ -n "$REQUESTED_LIFECYCLE_ID" ]] || input_error "--lifecycle-id is required"
  if lifecycle_recover "$CWD" "$REQUESTED_LIFECYCLE_ID"; then
    jq -nc --arg lifecycle_id "$REQUESTED_LIFECYCLE_ID" --arg coder "$LIFECYCLE_CODER" \
      --arg session "$LIFECYCLE_SESSION_ID" \
      '{status:"RECOVERED",coder:$coder,session_id:$session,lifecycle_id:$lifecycle_id,
        attempts:0,verification_mode:"none",verification:[],verified:false,changed:true,
        commit_id:"",files_changed:[],worktree_clean:false,writer_stopped:true,
        result:"",diagnostic:"quarantine cleared; resume the matching lifecycle"}'
    exit 0
  fi
  jq -nc --arg lifecycle_id "$REQUESTED_LIFECYCLE_ID" --arg diagnostic "$LIFECYCLE_DIAGNOSTIC" \
    '{status:"BLOCKED",coder:"",session_id:"",lifecycle_id:$lifecycle_id,
      attempts:0,verification_mode:"none",verification:[],verified:false,changed:false,
      commit_id:"",files_changed:[],worktree_clean:false,writer_stopped:false,
      result:"",diagnostic:$diagnostic}'
  exit 1
fi

CODER=""
TASK_FILE=""
VERIFY_COMMANDS=()
CWD=""
COMMIT_MESSAGE=""
MAX_RETRIES=3
SESSION=""
REQUESTED_LIFECYCLE_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --coder)
      [[ $# -ge 2 ]] || input_error "--coder requires a value"
      CODER="$2"; shift 2 ;;
    --task-file)
      [[ $# -ge 2 ]] || input_error "--task-file requires a value"
      TASK_FILE="$2"; shift 2 ;;
    --verify-cmd)
      [[ $# -ge 2 ]] || input_error "--verify-cmd requires a value"
      VERIFY_COMMANDS+=("$2"); shift 2 ;;
    --cwd)
      [[ $# -ge 2 ]] || input_error "--cwd requires a value"
      CWD="$2"; shift 2 ;;
    --commit-message)
      [[ $# -ge 2 ]] || input_error "--commit-message requires a value"
      COMMIT_MESSAGE="$2"; shift 2 ;;
    --max-retries)
      [[ $# -ge 2 ]] || input_error "--max-retries requires a value"
      MAX_RETRIES="$2"; shift 2 ;;
    --session)
      [[ $# -ge 2 ]] || input_error "--session requires a value"
      SESSION="$2"; shift 2 ;;
    --lifecycle-id)
      [[ $# -ge 2 ]] || input_error "--lifecycle-id requires a value"
      REQUESTED_LIFECYCLE_ID="$2"; shift 2 ;;
    *) input_error "unknown argument: $1" ;;
  esac
done

[[ -n "$CODER" ]] || input_error "--coder is required"
[[ -n "$TASK_FILE" && -r "$TASK_FILE" ]] || input_error "--task-file missing or unreadable"
[[ "${#VERIFY_COMMANDS[@]}" -gt 0 ]] \
  || input_error "--verify-cmd is required (use an empty value for no verification)"
[[ -n "$CWD" && -d "$CWD" ]] || input_error "--cwd missing or not a directory"
[[ -n "$COMMIT_MESSAGE" ]] || input_error "--commit-message is required"
[[ "$COMMIT_MESSAGE" != *$'\n'* && "$COMMIT_MESSAGE" != *$'\r'* ]] \
  || input_error "--commit-message must be one line"
[[ "$MAX_RETRIES" =~ ^[0-9]+$ ]] \
  || input_error "--max-retries must be a non-negative integer, got '$MAX_RETRIES'"
if [[ -n "$REQUESTED_LIFECYCLE_ID" \
      && ! "$REQUESTED_LIFECYCLE_ID" =~ ^[0-9]+-[0-9]+-[0-9]+$ ]]; then
  input_error "invalid lifecycle id"
fi

VERIFICATION_MODE="commands"
if [[ "${#VERIFY_COMMANDS[@]}" -eq 1 && -z "${VERIFY_COMMANDS[0]}" ]]; then
  VERIFICATION_MODE="none"
else
  for verify_command in "${VERIFY_COMMANDS[@]}"; do
    [[ -n "$verify_command" ]] \
      || input_error "empty verification cannot be mixed with verification commands"
    bash -n -c "$verify_command" >/dev/null 2>&1 \
      || input_error "invalid shell syntax in --verify-cmd"
    label_pattern='^[[:space:]]*[[:alnum:]_.-]+:([[:space:]]|$)'
    if [[ "$verify_command" =~ $label_pattern ]]; then
      input_error "verification commands cannot start with a label; remove text such as server: or client: and pass each command with its own --verify-cmd"
    fi
  done
fi

if ! lifecycle_resolve_paths "$CWD"; then
  input_error "$LIFECYCLE_DIAGNOSTIC"
fi
CWD="$LIFECYCLE_WORKTREE"

pool_load "$(pool_file_for coder)" coder
pool_get "$CODER"
harness_load "$ENTRY_HARNESS"

ERR_FILE=$(mktemp)
SESSION_ID=""
RESULT=""
STATUS="BLOCKED"
ATTEMPTS=0
VERIFY_RESULTS='[]'
VERIFIED=false
CHANGED=false
COMMIT_ID=""
FILES_CHANGED='[]'
WORKTREE_CLEAN=false
WRITER_STOPPED=true
DIAGNOSTIC=""
VERIFY_OUT=""
VERIFY_RC=0
VERIFY_FATAL=0
LIFECYCLE_OWNED=0
LIFECYCLE_FINALIZED=0

emit_result() {
  jq -nc --arg status "$STATUS" --arg coder "$CODER" --arg session "$SESSION_ID" \
    --arg lifecycle_id "$LIFECYCLE_ID" --argjson attempts "$ATTEMPTS" \
    --arg mode "$VERIFICATION_MODE" --argjson verification "$VERIFY_RESULTS" \
    --argjson verified "$VERIFIED" --argjson changed "$CHANGED" \
    --arg commit_id "$COMMIT_ID" --argjson files_changed "$FILES_CHANGED" \
    --argjson clean "$WORKTREE_CLEAN" --argjson stopped "$WRITER_STOPPED" \
    --arg result "$RESULT" --arg diagnostic "$DIAGNOSTIC" --arg vout "$VERIFY_OUT" \
    '{status:$status,coder:$coder,session_id:$session,lifecycle_id:$lifecycle_id,
      attempts:$attempts,verification_mode:$mode,verification:$verification,
      verified:$verified,changed:$changed,commit_id:$commit_id,
      files_changed:$files_changed,worktree_clean:$clean,writer_stopped:$stopped,
      result:$result,diagnostic:$diagnostic,verify_output:$vout}'
}

refresh_observed() {
  FILES_CHANGED="$(json_changed_paths)"
  if [[ "$(jq 'length' <<<"$FILES_CHANGED")" -gt 0 ]]; then CHANGED=true; else CHANGED=false; fi
  if [[ -z "$(git -C "$CWD" status --porcelain 2>/dev/null)" ]]; then
    WORKTREE_CLEAN=true
  else
    WORKTREE_CLEAN=false
  fi
}

unexpected_exit() {
  local rc=$?
  rm -f "$ERR_FILE"
  if [[ "$LIFECYCLE_OWNED" -eq 1 && "$LIFECYCLE_FINALIZED" -eq 0 ]]; then
    LIFECYCLE_ATTEMPTS="$ATTEMPTS"
    if [[ "$WRITER_STOPPED" == true ]]; then
      lifecycle_block_recoverable "delegate interrupted before completion" >/dev/null 2>&1 || true
    else
      lifecycle_quarantine "delegate interrupted while writer status was uncertain" >/dev/null 2>&1 || true
    fi
  fi
  return "$rc"
}
handle_signal() {
  local signal="$1" status="$2"
  trap - INT TERM
  DIAGNOSTIC="delegate interrupted by $signal"
  exit "$status"
}
trap unexpected_exit EXIT
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM

block_result() {
  DIAGNOSTIC="$1"
  local quarantine="${2:-0}"
  refresh_observed
  LIFECYCLE_ATTEMPTS="$ATTEMPTS"
  LIFECYCLE_SESSION_ID="$SESSION_ID"
  LIFECYCLE_PROCESS_GROUP_ID="$RUN_GROUP_ID"
  if [[ "$quarantine" -eq 1 || "$WRITER_STOPPED" != true ]]; then
    lifecycle_update quarantined "$SESSION_ID" "$RUN_GROUP_ID" "$WRITER_STOPPED" "$DIAGNOSTIC" >/dev/null 2>&1 || true
  else
    lifecycle_block_recoverable "$DIAGNOSTIC" >/dev/null 2>&1 || true
  fi
  STATUS="BLOCKED"
  emit_result
  LIFECYCLE_FINALIZED=1
  rm -f "$ERR_FILE"
  exit 1
}

if ! lifecycle_open "$CWD" "$CODER" "$REQUESTED_LIFECYCLE_ID"; then
  if [[ -r "$LIFECYCLE_STATE_FILE" ]]; then
    LIFECYCLE_ID="$(jq -r '.lifecycle_id // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    SESSION_ID="$(jq -r '.session_id // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    WRITER_STOPPED="$(jq -r '.writer_stopped // false' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  fi
  DIAGNOSTIC="$LIFECYCLE_DIAGNOSTIC"
  refresh_observed
  emit_result
  LIFECYCLE_FINALIZED=1
  rm -f "$ERR_FILE"
  exit 1
fi
LIFECYCLE_OWNED=1
RUN_GROUP_STOPPED=1
WRITER_STOPPED=true
RUN_GROUP_RECORD_FILE="$LIFECYCLE_LOCK_DIR/process_group_id"

if [[ -n "$REQUESTED_LIFECYCLE_ID" ]]; then
  if [[ -z "$SESSION" || -z "$LIFECYCLE_SESSION_ID" || "$SESSION" != "$LIFECYCLE_SESSION_ID" ]]; then
    SESSION_ID="$LIFECYCLE_SESSION_ID"
    block_result "resume requires the matching recorded session id"
  fi
  ATTEMPTS="${LIFECYCLE_ATTEMPTS:-0}"
fi

run_verify() {
  local verify_command f timed_out
  VERIFY_RC=0
  VERIFY_OUT=""
  VERIFY_FATAL=0
  VERIFY_RESULTS='[]'
  for verify_command in "${VERIFY_COMMANDS[@]}"; do
    f=$(mktemp)
    run_with_timeout_in "${CSC_VERIFY_TIMEOUT:-1800}" "$CWD" \
      bash -c "$verify_command" >"$f" 2>&1
    VERIFY_RC=$?
    VERIFY_OUT="$(cat "$f")"
    rm -f "$f"
    WRITER_STOPPED=$([[ "$RUN_GROUP_STOPPED" -eq 1 ]] && echo true || echo false)
    timed_out=false
    [[ "$TIMEOUT_HIT" -eq 1 ]] && timed_out=true
    if [[ "$VERIFY_RC" -ne 0 && -z "$VERIFY_OUT" ]]; then
      VERIFY_OUT="verification command exited with status $VERIFY_RC"
    fi
    if [[ "$timed_out" == true ]]; then
      VERIFY_OUT="verification timed out after ${CSC_VERIFY_TIMEOUT:-1800}s${VERIFY_OUT:+: $VERIFY_OUT}"
    fi
    VERIFY_RESULTS="$(jq -cn --argjson previous "$VERIFY_RESULTS" \
      --arg command "$verify_command" --argjson exit_code "$VERIFY_RC" \
      --argjson timed_out "$timed_out" --arg output "$VERIFY_OUT" \
      '$previous + [{command:$command,exit_code:$exit_code,timed_out:$timed_out,output:$output}]')"
    if [[ "$TIMEOUT_HIT" -eq 1 || "$VERIFY_RC" -eq "$LINGERING_EXIT" \
          || "$VERIFY_RC" -eq "$UNCONTAINED_EXIT" ]]; then
      VERIFY_FATAL=1
    fi
    [[ "$VERIFY_RC" -eq 0 ]] || break
  done
}

run_coder() {
  local prompt="$1" session="$2" rc
  HARNESS_TIMED_OUT=0
  RUN_GROUP_STOPPED=0
  harness_run "edit" "$ENTRY_MODEL" "$CWD" "$prompt" "$session"
  rc=$?
  WRITER_STOPPED=$([[ "$RUN_GROUP_STOPPED" -eq 1 ]] && echo true || echo false)
  LIFECYCLE_PROCESS_GROUP_ID="$RUN_GROUP_ID"
  LIFECYCLE_SESSION_ID="$SESSION_ID"
  LIFECYCLE_ATTEMPTS="$ATTEMPTS"
  lifecycle_update active "$SESSION_ID" "$RUN_GROUP_ID" "$WRITER_STOPPED" "" >/dev/null 2>&1 || true
  return "$rc"
}

PROMPT="Read the file $TASK_FILE and implement the task it describes.

Edit the files needed for the task. Do not commit, stash, switch branches, reset,
clean, restore files, or run any other Git command that changes repository state.
The caller owns Git state and will commit verified work."

if ! run_coder "$PROMPT" "$SESSION"; then
  diagnostic="coder invocation failed: $(cat "$ERR_FILE" 2>/dev/null)"
  VERIFY_OUT="$diagnostic"
  block_result "$diagnostic"
fi
[[ -n "$SESSION_ID" ]] || block_result "coder returned no session id"

if [[ "$VERIFICATION_MODE" == "none" ]]; then
  VERIFIED=false
else
  run_verify
  while [[ "$VERIFY_RC" -ne 0 && "$VERIFY_FATAL" -eq 0 && "$ATTEMPTS" -lt "$MAX_RETRIES" ]]; do
    ATTEMPTS=$((ATTEMPTS+1))
    fix_prompt="The verification command failed with this output:

$VERIFY_OUT

Fix the code so the verification passes. Make all necessary edits. Do not commit,
stash, switch branches, reset, clean, restore files, or run any other Git command
that changes repository state. The caller owns Git state."
    previous_session="$SESSION_ID"
    if ! run_coder "$fix_prompt" "$previous_session"; then
      diagnostic="coder failed during fix attempt $ATTEMPTS: $(cat "$ERR_FILE" 2>/dev/null)"
      [[ -n "$SESSION_ID" ]] || SESSION_ID="$previous_session"
      block_result "$diagnostic"
    fi
    [[ -n "$SESSION_ID" ]] || { SESSION_ID="$previous_session"; block_result "coder returned no session id on fix attempt $ATTEMPTS"; }
    run_verify
  done
  if [[ "$VERIFY_RC" -ne 0 ]]; then
    block_result "$VERIFY_OUT"
  fi
  VERIFIED=true
fi

current_branch="$(git -C "$CWD" branch --show-current 2>/dev/null)"
current_commit="$(git -C "$CWD" rev-parse HEAD 2>/dev/null)"
if [[ "$current_branch" != "$LIFECYCLE_START_BRANCH" || "$current_commit" != "$LIFECYCLE_START_COMMIT" ]]; then
  block_result "coder changed the branch or commit; inspect the preserved worktree before recovery" 1
fi

refresh_observed
if [[ "$CHANGED" == true ]]; then
  if ! git -C "$CWD" add -A 2>>"$ERR_FILE"; then
    block_result "failed to stage delegated changes: $(cat "$ERR_FILE")"
  fi
  if ! git -C "$CWD" commit -m "$COMMIT_MESSAGE" >"$ERR_FILE" 2>&1; then
    observed_head="$(git -C "$CWD" rev-parse HEAD 2>/dev/null)"
    if [[ "$observed_head" != "$LIFECYCLE_START_COMMIT" ]]; then
      COMMIT_ID="$observed_head"
    fi
    block_result "failed to commit delegated changes: $(cat "$ERR_FILE")"
  fi
  COMMIT_ID="$(git -C "$CWD" rev-parse HEAD)"
fi

refresh_observed
if [[ "$WORKTREE_CLEAN" != true ]]; then
  block_result "commit completed but the worktree is not clean"
fi

if [[ -z "$SESSION_ID" || "$WRITER_STOPPED" != true || "$WORKTREE_CLEAN" != true \
      || ( "$VERIFICATION_MODE" != none && "$VERIFIED" != true ) \
      || ( "$CHANGED" == true && -z "$COMMIT_ID" ) ]]; then
  block_result "completion invariants were not satisfied"
fi

STATUS="DONE"
DIAGNOSTIC=""
if ! lifecycle_finish; then
  STATUS="BLOCKED"
  DIAGNOSTIC="task completed but lifecycle ownership could not be released"
  refresh_observed
  emit_result
  LIFECYCLE_FINALIZED=1
  rm -f "$ERR_FILE"
  exit 1
fi
LIFECYCLE_FINALIZED=1
emit_result
rm -f "$ERR_FILE"
exit 0
