#!/usr/bin/env bash
# cr-delegate.sh — delegate ONE design-spec or plan review to <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->
# via cursor-agent in read-only mode, and return its report.
# Output discipline: ONLY the final STATUS JSON goes to stdout; diagnostics/progress to stderr.
set -uo pipefail

CURSOR_BIN="${CR_CURSOR_BIN:-cursor-agent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_JSON="${CR_MODELS_JSON:-$SCRIPT_DIR/../.claude-plugin/models.json}"
MODEL="$(jq -r '.reviewer.id // empty' "$MODELS_JSON" 2>/dev/null)"
if [[ -z "$MODEL" ]]; then
  echo "error: could not read .reviewer.id from $MODELS_JSON" >&2
  exit 2
fi

usage() {
  echo "usage: cr-delegate.sh --target spec|plan --doc-file <path> [--spec-file <path>] [--lenses backend,frontend,ui] [--rubric-dir <path>] [--session <id>]" >&2
}

TARGET=""
DOC_FILE=""
SPEC_FILE=""
LENSES=""
RUBRIC_DIR="$SCRIPT_DIR/../rubrics"
SESSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)     TARGET="${2:-}"; shift 2 ;;
    --doc-file)   DOC_FILE="${2:-}"; shift 2 ;;
    --spec-file)  SPEC_FILE="${2:-}"; shift 2 ;;
    --lenses)     LENSES="${2:-}"; shift 2 ;;
    --rubric-dir) RUBRIC_DIR="${2:-}"; shift 2 ;;
    --session)    SESSION="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

# --- arg validation ---
if [[ "$TARGET" != "spec" && "$TARGET" != "plan" ]]; then
  echo "error: --target must be 'spec' or 'plan'" >&2; usage; exit 2
fi
if [[ -z "$DOC_FILE" || ! -r "$DOC_FILE" ]]; then
  echo "error: --doc-file missing or unreadable" >&2; usage; exit 2
fi
if [[ -n "$SPEC_FILE" && ! -r "$SPEC_FILE" ]]; then
  echo "error: --spec-file given but unreadable: $SPEC_FILE" >&2; usage; exit 2
fi

# Map target -> rubric file; both target rubric and the output format are required.
TARGET_RUBRIC="$RUBRIC_DIR/${TARGET}-review.md"
OUTPUT_FMT="$RUBRIC_DIR/_output-format.md"
for f in "$TARGET_RUBRIC" "$OUTPUT_FMT"; do
  if [[ ! -r "$f" ]]; then
    echo "error: required rubric file missing: $f" >&2; exit 2
  fi
done

# Parse + validate lenses (spec reviews). Unknown lens or missing file -> exit 2.
LENS_FILES=()
if [[ -n "$LENSES" ]]; then
  IFS=',' read -ra _lenses <<< "$LENSES"
  for lens in "${_lenses[@]}"; do
    [[ -z "$lens" ]] && continue
    case "$lens" in
      backend|frontend|ui) ;;
      *) echo "error: unknown lens '$lens' (allowed: backend, frontend, ui)" >&2; exit 2 ;;
    esac
    lf="$RUBRIC_DIR/lens-${lens}.md"
    if [[ ! -r "$lf" ]]; then
      echo "error: lens rubric file missing: $lf" >&2; exit 2
    fi
    LENS_FILES+=("$lf")
  done
fi

# --- assemble the review prompt (inlines rubrics; points the reviewer at the doc) ---
assemble_prompt() {
  cat <<EOF
You are an independent, senior software reviewer. You did NOT author the document
under review — your job is to challenge it rigorously and surface problems before
implementation. You have READ-ONLY access to this repository: explore the existing
code as needed to judge how the proposed work fits the current codebase and whether
it risks breaking existing behavior. Do not attempt to modify any files.

The document under review is at: $DOC_FILE
Read it in full before reviewing.
EOF
  if [[ -n "$SPEC_FILE" ]]; then
    echo "The reference spec this document must satisfy is at: $SPEC_FILE"
    echo "Read it too and check the document against it."
  fi
  echo
  echo "Apply the following review rubric:"
  echo
  cat "$TARGET_RUBRIC"
  if [[ ${#LENS_FILES[@]} -gt 0 ]]; then
    for lf in "${LENS_FILES[@]}"; do
      echo
      cat "$lf"
    done
  fi
  echo
  echo "Produce your review in EXACTLY the following format:"
  echo
  cat "$OUTPUT_FMT"
}

PROMPT="$(assemble_prompt)"

# --- run cursor-agent once (read-only), render progress to stderr, keep result event ---
ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT
cursor_err() { cat "$ERR_FILE" 2>/dev/null; }

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
  # --mode ask: read-only Q&A. --approve-mcps: never block on MCP approval headless.
  local -a cmd=("$CURSOR_BIN" -p --force --trust --approve-mcps --mode ask
                --output-format stream-json --model "$MODEL")
  [[ -n "$sess" ]] && cmd+=(--resume="$sess")
  cmd+=("$prompt")

  "${cmd[@]}" >"$outfile" 2>"$ERR_FILE"
  rc=$?

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
  if [[ -z "$result_line" ]] || ! echo "$result_line" | jq -e . >/dev/null 2>&1; then
    return 1
  fi
  local is_err sub
  is_err=$(echo "$result_line" | jq -r '.is_error // false')
  sub=$(echo "$result_line" | jq -r '.subtype // ""')
  if [[ "$is_err" == "true" || ( -n "$sub" && "$sub" != "success" ) ]]; then
    echo "$result_line" | jq -r '.result // "cursor-agent reported is_error"' > "$ERR_FILE"
    return 1
  fi
  echo "$result_line"
}

emit() {  # emit <status> <session> <target> <lenses_csv> <report> <diagnostic>
  jq -nc \
    --arg status "$1" --arg session "$2" --arg target "$3" \
    --arg lenses "$4" --arg report "$5" --arg diag "$6" \
    '{status:$status, session_id:$session, target:$target,
      lenses:($lenses|split(",")|map(select(length>0))),
      report:$report, diagnostic:$diag}'
}

CURSOR_OUT=$(run_cursor "$PROMPT" "$SESSION") || {
  emit "BLOCKED" "$SESSION" "$TARGET" "$LENSES" "" "cursor-agent invocation failed: $(cursor_err)"
  exit 1
}

SESSION_ID=$(echo "$CURSOR_OUT" | jq -r '.session_id // ""')
REPORT=$(echo "$CURSOR_OUT" | jq -r '.result // ""')

if [[ -z "$REPORT" ]]; then
  emit "BLOCKED" "$SESSION_ID" "$TARGET" "$LENSES" "" "cursor-agent returned an empty review"
  exit 1
fi

emit "REVIEWED" "$SESSION_ID" "$TARGET" "$LENSES" "$REPORT" ""
exit 0
