#!/usr/bin/env bash
# codex.sh — how to drive the Codex CLI. Sourced by the delegate scripts.
# Writes nothing to stdout.

CSC_CODEX_BIN="${CSC_CODEX_BIN:-codex}"

harness_render() {  # <json-line>
  local line="$1" type itype text
  type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null) || return 0
  [[ "$type" == "item.completed" ]] || return 0
  itype=$(jq -r '.item.type // ""' <<<"$line" 2>/dev/null)
  [[ "$itype" == "agent_message" ]] && return 0
  text=$(jq -r '.item.text // .item.command // ""' <<<"$line" 2>/dev/null)
  echo "  -> ${itype}${text:+ ($text)}" >&2
}

harness_probe() {
  local model="$1" out rc
  PROBE_REASON=""
  if ! command -v "$CSC_CODEX_BIN" >/dev/null 2>&1 && [[ ! -x "$CSC_CODEX_BIN" ]]; then
    PROBE_REASON="not-installed"; echo "codex not found: $CSC_CODEX_BIN" >&2; return 1
  fi
  out=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$out'" RETURN
  run_with_timeout "${CSC_PROBE_TIMEOUT:-120}" \
    "$CSC_CODEX_BIN" exec --json -s read-only -m "$model" \
    "Reply with the single word READY." >"$out" 2>>"$ERR_FILE" </dev/null
  rc=$?
  if [[ $rc -eq "$TIMEOUT_EXIT" ]]; then PROBE_REASON="timeout"; return 1; fi
  # Parse for a real turn.failed EVENT. A substring grep would also match the
  # phrase appearing inside the model's own answer text.
  local failed
  failed=$(jq -rs '[.[] | select(.type=="turn.failed")] | length' "$out" 2>/dev/null || echo 0)
  if [[ $rc -ne 0 ]] || [[ "${failed:-0}" != "0" ]]; then
    PROBE_REASON="$(harness_classify "$(cat "$ERR_FILE" 2>/dev/null; cat "$out")")"
    cat "$out" >> "$ERR_FILE"; return 1
  fi
  local answer
  answer=$(jq -r 'select(.type=="item.completed") | select(.item.type=="agent_message") | .item.text' "$out" 2>/dev/null | tail -1)
  if ! harness_is_ready "$answer"; then
    PROBE_REASON="other"; cat "$out" >> "$ERR_FILE"; return 1
  fi
  return 0
}

harness_run() {
  local mode="$1" model="$2" dir="$3" prompt="$4" sess="$5"
  local outfile rc line type itype turn_failed="" fail_msg=""
  : > "$ERR_FILE"
  outfile=$(mktemp)
  SESSION_ID=""
  RESULT=""

  local -a cmd
  if [[ -n "$sess" ]]; then
    # VERIFIED 2026-08-22: `codex exec resume` has NO -C and NO -s/--sandbox.
    # Without the -c override below, a resumed read-only call would fall back
    # to the user's config.toml sandbox and could gain WRITE access.
    cmd=("$CSC_CODEX_BIN" exec resume "$sess" --json -m "$model")
    [[ "$mode" == "read-only" ]] && cmd+=(-c 'sandbox_mode="read-only"')
  else
    cmd=("$CSC_CODEX_BIN" exec --json -C "$dir" -m "$model")
    [[ "$mode" == "read-only" ]] && cmd+=(-s read-only)
  fi
  cmd+=("$prompt")

  # resume has no -C, so enter $dir for both paths.
  ( cd "$dir" && run_with_timeout "${CSC_RUN_TIMEOUT:-1800}" "${cmd[@]}" </dev/null ) \
    >"$outfile" 2>>"$ERR_FILE"
  rc=$?

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null)
    case "$type" in
      thread.started)
        [[ -z "$SESSION_ID" ]] && SESSION_ID=$(jq -r '.thread_id // ""' <<<"$line" 2>/dev/null)
        ;;
      item.completed)
        itype=$(jq -r '.item.type // ""' <<<"$line" 2>/dev/null)
        if [[ "$itype" == "agent_message" ]]; then
          RESULT=$(jq -r '.item.text // ""' <<<"$line" 2>/dev/null)
        else
          harness_render "$line"
        fi
        ;;
      turn.failed)
        turn_failed=1
        fail_msg=$(jq -r '.error.message // "codex reported turn.failed"' <<<"$line" 2>/dev/null)
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
  if [[ -n "$turn_failed" ]]; then echo "$fail_msg" > "$ERR_FILE"; return 1; fi
  [[ $rc -ne 0 ]] && return 1
  return 0
}
