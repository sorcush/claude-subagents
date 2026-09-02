#!/usr/bin/env bash
# cursor.sh — how to drive `cursor-agent`. Sourced by the delegate scripts.
# Writes nothing to stdout. See the spec's "Calling rules" section.

CSC_CURSOR_BIN="${CSC_CURSOR_BIN:-cursor-agent}"

harness_render() {  # <json-line>
  local line="$1" type sub tool path text
  type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null) || return 0
  case "$type" in
    assistant)
      text=$(jq -r '.message.content[]? | select(.type=="text") | .text' <<<"$line" 2>/dev/null)
      [[ -n "$text" ]] && echo "  . $text" >&2
      ;;
    tool_call)
      sub=$(jq -r '.subtype // ""' <<<"$line" 2>/dev/null)
      tool=$(jq -r '.tool_call | keys[0] // "tool"' <<<"$line" 2>/dev/null)
      path=$(jq -r '.tool_call[]?.args.path // ""' <<<"$line" 2>/dev/null)
      echo "  -> ${tool} ${sub}${path:+ ($path)}" >&2
      ;;
  esac
}

# harness_probe <model>
harness_probe() {
  local model="$1" out rc
  PROBE_REASON=""
  if ! command -v "$CSC_CURSOR_BIN" >/dev/null 2>&1 && [[ ! -x "$CSC_CURSOR_BIN" ]]; then
    PROBE_REASON="not-installed"; echo "cursor-agent not found: $CSC_CURSOR_BIN" >&2; return 1
  fi
  out=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$out'" RETURN
  run_with_timeout "${CSC_PROBE_TIMEOUT:-120}" \
    "$CSC_CURSOR_BIN" -p --force --trust --mode ask --output-format stream-json --model "$model" \
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
  # Compare the tool's actual answer, not the raw stream.
  local answer
  answer=$(jq -r 'select(.type=="result") | .result // ""' "$out" 2>/dev/null | tail -1)
  if ! harness_is_ready "$answer"; then
    PROBE_REASON="other"; cat "$out" >> "$ERR_FILE"; return 1
  fi
  return 0
}

# harness_run <mode> <model> <dir> <prompt> <session>
# Sets SESSION_ID and RESULT. Call as a plain statement, never in $(...).
harness_run() {
  local mode="$1" model="$2" dir="$3" prompt="$4" sess="$5"
  local outfile rc line result_line="" is_err sub
  : > "$ERR_FILE"
  outfile=$(mktemp)
  # Reset before every call. Without this, a failed resume could leave the
  # PREVIOUS call's session id in place and look like a success.
  SESSION_ID=""
  RESULT=""

  local -a cmd=("$CSC_CURSOR_BIN" -p --force --trust --approve-mcps
                --output-format stream-json --model "$model")
  [[ "$mode" == "read-only" ]] && cmd+=(--mode ask)
  [[ -n "$sess" ]] && cmd+=(--resume="$sess")
  cmd+=("$prompt")

  # cursor-agent has no working-folder flag, so enter $dir ourselves.
  ( cd "$dir" && run_with_timeout "${CSC_RUN_TIMEOUT:-1800}" "${cmd[@]}" </dev/null ) \
    >"$outfile" 2>>"$ERR_FILE"
  rc=$?

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "$(jq -r '.type // ""' <<<"$line" 2>/dev/null)" == "result" ]]; then
      result_line="$line"
    else
      harness_render "$line"
    fi
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
    jq -r '.result // "cursor-agent reported is_error"' <<<"$result_line" > "$ERR_FILE"
    return 1
  fi

  SESSION_ID=$(jq -r '.session_id // ""' <<<"$result_line")
  RESULT=$(jq -r '.result // ""' <<<"$result_line")
  return 0
}
