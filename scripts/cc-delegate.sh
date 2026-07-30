#!/usr/bin/env bash
# cc-delegate.sh — delegate one task to Cursor <!-- model:coder:label -->Composer 2.5<!-- /model:coder:label --> and verify.
# Output discipline: ONLY the final STATUS JSON goes to stdout; diagnostics to stderr.
set -uo pipefail

CURSOR_BIN="${CC_CURSOR_BIN:-cursor-agent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_JSON="${CC_MODELS_JSON:-$SCRIPT_DIR/../.claude-plugin/models.json}"
MODEL="$(jq -r '.coder.id // empty' "$MODELS_JSON" 2>/dev/null)"
if [[ -z "$MODEL" ]]; then
  echo "error: could not read .coder.id from $MODELS_JSON" >&2
  exit 2
fi

usage() {
  echo "usage: cc-delegate.sh --task-file <path> --verify-cmd <cmd> [--max-retries N] [--session <id>]" >&2
}

TASK_FILE=""
VERIFY_CMD=""
VERIFY_CMD_SET=0
MAX_RETRIES=3
SESSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task-file)   TASK_FILE="${2:-}"; shift 2 ;;
    --verify-cmd)  VERIFY_CMD="${2:-}"; VERIFY_CMD_SET=1; shift 2 ;;
    --max-retries) MAX_RETRIES="${2:-3}"; shift 2 ;;
    --session)     SESSION="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$TASK_FILE" || ! -r "$TASK_FILE" ]]; then
  echo "error: --task-file missing or unreadable" >&2; usage; exit 2
fi
if [[ "$VERIFY_CMD_SET" -ne 1 ]]; then
  echo "error: --verify-cmd is required (use \"\" for no verification)" >&2; usage; exit 2
fi

# --- helper: run cursor-agent once, echo JSON on success, return non-zero on failure ---
# run_cursor is called in a command substitution (subshell), so it cannot export
# state via a variable. On failure it writes the real diagnostic (cursor-agent
# stderr, or the error result) to $ERR_FILE, which the parent reads via cursor_err().
ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT
cursor_err() { cat "$ERR_FILE" 2>/dev/null; }

# Render one stream-json event to stderr as a human-readable progress line.
render_event() {  # render_event <json-line>
  local line="$1" type sub
  type=$(echo "$line" | jq -r '.type // ""' 2>/dev/null) || return 0
  case "$type" in
    assistant)
      local text
      text=$(echo "$line" | jq -r '.message.content[]? | select(.type=="text") | .text' 2>/dev/null)
      [[ -n "$text" ]] && echo "  · $text" >&2
      ;;
    tool_call)
      sub=$(echo "$line" | jq -r '.subtype // ""' 2>/dev/null)
      # Tool name is the KEY inside .tool_call (e.g. readToolCall); path is in its args.
      local tool path
      tool=$(echo "$line" | jq -r '.tool_call | keys[0] // "tool"' 2>/dev/null)
      path=$(echo "$line" | jq -r '.tool_call[]?.args.path // ""' 2>/dev/null)
      echo "  → ${tool} ${sub}${path:+ ($path)}" >&2
      ;;
  esac
}

run_cursor() {  # run_cursor <prompt> [session_id]
  local prompt="$1" sess="${2:-}"
  local rc result_line="" outfile
  : > "$ERR_FILE"
  outfile=$(mktemp)
  # --approve-mcps: never block on MCP-server approval prompts in headless mode.
  # stream-json gives per-event progress (rendered to stderr); the terminal
  # `result` event carries the same shape the rest of the script consumes.
  local -a cmd=("$CURSOR_BIN" -p --force --trust --approve-mcps
                --output-format stream-json --model "$MODEL")
  [[ -n "$sess" ]] && cmd+=(--resume="$sess")
  cmd+=("$prompt")

  "${cmd[@]}" >"$outfile" 2>"$ERR_FILE"
  rc=$?

  # Read events line-by-line: render progress to stderr, keep the result event.
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "$(echo "$line" | jq -r '.type // ""' 2>/dev/null)" == "result" ]]; then
      result_line="$line"
    else
      render_event "$line"
    fi
  done < "$outfile"
  rm -f "$outfile"

  if [[ $rc -ne 0 ]]; then return 1; fi
  # No terminal result event (stream ended early) is a failure.
  if [[ -z "$result_line" ]] || ! echo "$result_line" | jq -e . >/dev/null 2>&1; then
    return 1
  fi
  # well-formed JSON that reports an error is also a failure (is_error / subtype).
  local is_err sub
  is_err=$(echo "$result_line" | jq -r '.is_error // false')
  sub=$(echo "$result_line" | jq -r '.subtype // ""')
  if [[ "$is_err" == "true" || ( -n "$sub" && "$sub" != "success" ) ]]; then
    echo "$result_line" | jq -r '.result // "cursor-agent reported is_error"' > "$ERR_FILE"
    return 1
  fi
  echo "$result_line"
}

emit() {  # emit <status> <session> <attempts> <verified> <result> <verify_output>
  jq -nc \
    --arg status "$1" --arg session "$2" --argjson attempts "$3" \
    --argjson verified "$4" --arg result "$5" --arg vout "$6" \
    '{status:$status, session_id:$session, attempts:$attempts, verified:$verified, result:$result, verify_output:$vout}'
}

PROMPT="Read the file $TASK_FILE and implement the task it describes. Make all necessary code edits."

CURSOR_OUT=$(run_cursor "$PROMPT" "$SESSION") || {
  emit "BLOCKED" "$SESSION" 0 false "" "cursor-agent invocation failed: $(cursor_err)"
  exit 1
}

SESSION_ID=$(echo "$CURSOR_OUT" | jq -r '.session_id // ""')
RESULT_TEXT=$(echo "$CURSOR_OUT" | jq -r '.result // ""')

# No verify command -> trust Composer, report DONE/unverified.
if [[ -z "$VERIFY_CMD" ]]; then
  emit "DONE" "$SESSION_ID" 0 false "$RESULT_TEXT" ""
  exit 0
fi

# Verify, and on failure resume the same session up to MAX_RETRIES times.
attempts=0
verify_out=$(eval "$VERIFY_CMD" 2>&1)
if [[ $? -eq 0 ]]; then
  emit "DONE" "$SESSION_ID" "$attempts" true "$RESULT_TEXT" "$verify_out"
  exit 0
fi

while [[ $attempts -lt $MAX_RETRIES ]]; do
  attempts=$((attempts+1))
  fix_prompt="The verification command failed with this output:

$verify_out

Fix the code so the verification passes. Make all necessary edits."
  CURSOR_OUT=$(run_cursor "$fix_prompt" "$SESSION_ID") || {
    emit "BLOCKED" "$SESSION_ID" "$attempts" false "$RESULT_TEXT" "cursor-agent failed during fix attempt $attempts: $(cursor_err)"
    exit 1
  }
  SESSION_ID=$(echo "$CURSOR_OUT" | jq -r '.session_id // ""')
  RESULT_TEXT=$(echo "$CURSOR_OUT" | jq -r '.result // ""')
  verify_out=$(eval "$VERIFY_CMD" 2>&1)
  if [[ $? -eq 0 ]]; then
    emit "DONE" "$SESSION_ID" "$attempts" true "$RESULT_TEXT" "$verify_out"
    exit 0
  fi
done

emit "BLOCKED" "$SESSION_ID" "$attempts" false "$RESULT_TEXT" "$verify_out"
exit 1
