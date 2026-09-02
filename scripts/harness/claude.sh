#!/usr/bin/env bash
# claude.sh — how to drive the Claude CLI headlessly. Sourced by the delegates.
# This starts a SEPARATE `claude -p` process; it is not an in-session subagent,
# which is what makes it usable as an independent reviewer.
# Writes nothing to stdout.

CSC_CLAUDE_BIN="${CSC_CLAUDE_BIN:-claude}"

harness_render() {  # <json-line>
  local line="$1" type text
  type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null) || return 0
  [[ "$type" == "assistant" ]] || return 0
  text=$(jq -r '.message.content[]? | select(.type=="text") | .text' <<<"$line" 2>/dev/null)
  [[ -n "$text" ]] && echo "  . $text" >&2
}

harness_probe() {
  local model="$1" out rc
  PROBE_REASON=""
  if ! command -v "$CSC_CLAUDE_BIN" >/dev/null 2>&1 && [[ ! -x "$CSC_CLAUDE_BIN" ]]; then
    PROBE_REASON="not-installed"; echo "claude not found: $CSC_CLAUDE_BIN" >&2; return 1
  fi
  out=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$out'" RETURN
  run_with_timeout "${CSC_PROBE_TIMEOUT:-120}" \
    "$CSC_CLAUDE_BIN" -p --model "$model" --output-format stream-json \
    --allowedTools "Read" --permission-mode dontAsk \
    "Reply with the single word READY." >"$out" 2>>"$ERR_FILE" </dev/null
  rc=$?
  if [[ $rc -eq "$TIMEOUT_EXIT" ]]; then PROBE_REASON="timeout"; return 1; fi
  if [[ $rc -ne 0 ]]; then
    PROBE_REASON="$(harness_classify "$(cat "$ERR_FILE" 2>/dev/null)")"
    return 1
  fi
  # These tools can exit 0 and still report failure inside the result line — that is
  # how a bad model id arrives. Without this, harness_classify never runs and the user
  # is told the wrong thing to fix.
  local is_err
  is_err=$(jq -r 'select(.type=="result") | .is_error // false' "$out" 2>/dev/null | tail -1)
  if [[ "$is_err" == "true" ]]; then
    PROBE_REASON="$(harness_classify "$(jq -r 'select(.type=="result") | .result // ""' "$out" 2>/dev/null | tail -1)")"
    cat "$out" >> "$ERR_FILE"
    return 1
  fi
  local answer
  answer=$(jq -r 'select(.type=="result") | .result // ""' "$out" 2>/dev/null | tail -1)
  if ! harness_is_ready "$answer"; then
    PROBE_REASON="other"; cat "$out" >> "$ERR_FILE"; return 1
  fi
  return 0
}

harness_run() {
  local mode="$1" model="$2" dir="$3" prompt="$4" sess="$5"
  local outfile rc line result_line="" is_err sub
  : > "$ERR_FILE"
  outfile=$(mktemp)
  # Reset before every call, for the same reason as the cursor harness.
  SESSION_ID=""
  RESULT=""

  local -a cmd=("$CSC_CLAUDE_BIN" -p --model "$model" --output-format stream-json)
  if [[ "$mode" == "read-only" ]]; then
    cmd+=(--allowedTools "Read Grep Glob" --permission-mode dontAsk)
  else
    cmd+=(--permission-mode acceptEdits)
  fi
  [[ -n "$sess" ]] && cmd+=(--resume "$sess")
  cmd+=("$prompt")

  # claude has no working-folder flag, so enter $dir ourselves.
  ( cd "$dir" && run_with_timeout "${CSC_RUN_TIMEOUT:-1800}" "${cmd[@]}" </dev/null ) \
    >"$outfile" 2>>"$ERR_FILE"
  rc=$?

  local type
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null)
    case "$type" in
      result)
        result_line="$line"
        ;;
      system)
        # The init event announces session_id before any work happens, so a
        # run killed by the timeout below still leaves a resumable session id.
        if [[ -z "$SESSION_ID" && "$(jq -r '.subtype // ""' <<<"$line" 2>/dev/null)" == "init" ]]; then
          SESSION_ID=$(jq -r '.session_id // ""' <<<"$line" 2>/dev/null)
        fi
        ;;
      *)
        harness_render "$line"
        ;;
    esac
  done < "$outfile"
  rm -f "$outfile"

  # 124 is how run_with_timeout reports a timeout across the subshell above.
  # A variable set inside that subshell would have been discarded.
  if [[ $rc -eq "$TIMEOUT_EXIT" ]]; then
    echo "timed out after ${CSC_RUN_TIMEOUT:-1800}s" >> "$ERR_FILE"
    HARNESS_TIMED_OUT=1
    return 1
  fi
  [[ $rc -ne 0 ]] && return 1
  [[ -z "$result_line" ]] && return 1
  jq -e . <<<"$result_line" >/dev/null 2>&1 || return 1

  is_err=$(jq -r '.is_error // false' <<<"$result_line")
  sub=$(jq -r '.subtype // ""' <<<"$result_line")
  if [[ "$is_err" == "true" || ( -n "$sub" && "$sub" != "success" ) ]]; then
    jq -r '.result // "claude reported is_error"' <<<"$result_line" > "$ERR_FILE"
    return 1
  fi

  SESSION_ID=$(jq -r '.session_id // ""' <<<"$result_line")
  RESULT=$(jq -r '.result // ""' <<<"$result_line")
  return 0
}
