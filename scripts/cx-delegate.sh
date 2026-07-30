#!/usr/bin/env bash
# cx-delegate.sh — delegate ONE design-spec or plan review to <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->
# via the Codex CLI (`codex exec`) in a read-only sandbox, and return its report.
# Output discipline: ONLY the final STATUS JSON goes to stdout; diagnostics/progress to stderr.
set -uo pipefail

CODEX_BIN="${CX_CODEX_BIN:-codex}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_JSON="${CX_MODELS_JSON:-$SCRIPT_DIR/../.claude-plugin/models.json}"
MODEL="$(jq -r '.codex_reviewer.id // empty' "$MODELS_JSON" 2>/dev/null)"
if [[ -z "$MODEL" ]]; then
  echo "error: could not read .codex_reviewer.id from $MODELS_JSON" >&2
  exit 2
fi

usage() {
  echo "usage: cx-delegate.sh --target spec|plan --doc-file <path> [--spec-file <path>] [--lenses backend,frontend,ui] [--rubric-dir <path>] [--session <id>] [--repo-root <path>]" >&2
}

TARGET=""
DOC_FILE=""
SPEC_FILE=""
LENSES=""
RUBRIC_DIR="$SCRIPT_DIR/../rubrics"
SESSION=""
REPO_ROOT_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)     TARGET="${2:-}"; shift 2 ;;
    --doc-file)   DOC_FILE="${2:-}"; shift 2 ;;
    --spec-file)  SPEC_FILE="${2:-}"; shift 2 ;;
    --lenses)     LENSES="${2:-}"; shift 2 ;;
    --rubric-dir) RUBRIC_DIR="${2:-}"; shift 2 ;;
    --session)    SESSION="${2:-}"; shift 2 ;;
    --repo-root)  REPO_ROOT_ARG="${2:-}"; shift 2 ;;
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
if [[ -n "$REPO_ROOT_ARG" && ! -d "$REPO_ROOT_ARG" ]]; then
  echo "error: --repo-root is not a directory: $REPO_ROOT_ARG" >&2; exit 2
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

# --- resolve the Codex working root (grounds "explore the existing repo" in a real dir) ---
resolve_repo_root() {
  if [[ -n "$REPO_ROOT_ARG" ]]; then
    REPO_ROOT="$(cd "$REPO_ROOT_ARG" && pwd)"
    return
  fi
  local doc_dir git_root
  doc_dir="$(cd "$(dirname "$DOC_FILE")" && pwd)"
  if git_root=$(git -C "$doc_dir" rev-parse --show-toplevel 2>/dev/null); then
    REPO_ROOT="$git_root"
  else
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  fi
}
resolve_repo_root

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

# --- run codex once (read-only), render progress to stderr, capture session + report ---
ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT

render_event() {  # render_event <json-line> — best-effort progress; unknown types ignored
  local line="$1" type
  type=$(echo "$line" | jq -r '.type // ""' 2>/dev/null) || return 0
  case "$type" in
    item.completed)
      local itype text
      itype=$(echo "$line" | jq -r '.item.type // ""' 2>/dev/null)
      [[ "$itype" == "agent_message" ]] && return 0
      text=$(echo "$line" | jq -r '.item.text // .item.command // ""' 2>/dev/null)
      echo "  -> ${itype}${text:+ ($text)}" >&2
      ;;
  esac
}

SESSION_ID=""
REPORT=""

run_codex() {  # run_codex <prompt> [session_id] — sets SESSION_ID / REPORT, returns 1 on failure
  local prompt="$1" sess="${2:-}"
  local rc outfile turn_failed="" fail_msg=""
  : > "$ERR_FILE"
  outfile=$(mktemp)
  local -a cmd
  if [[ -n "$sess" ]]; then
    cmd=("$CODEX_BIN" exec resume "$sess" --json -m "$MODEL" "$prompt")
  else
    cmd=("$CODEX_BIN" exec --json -s read-only -C "$REPO_ROOT" -m "$MODEL" "$prompt")
  fi
  "${cmd[@]}" < /dev/null >"$outfile" 2>"$ERR_FILE"
  rc=$?

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local type
    type=$(echo "$line" | jq -r '.type // ""' 2>/dev/null)
    case "$type" in
      thread.started)
        [[ -z "$SESSION_ID" ]] && SESSION_ID=$(echo "$line" | jq -r '.thread_id // ""' 2>/dev/null)
        ;;
      item.completed)
        local itype
        itype=$(echo "$line" | jq -r '.item.type // ""' 2>/dev/null)
        if [[ "$itype" == "agent_message" ]]; then
          REPORT=$(echo "$line" | jq -r '.item.text // ""' 2>/dev/null)
        else
          render_event "$line"
        fi
        ;;
      turn.failed)
        turn_failed=1
        fail_msg=$(echo "$line" | jq -r '.error.message // "codex exec reported turn.failed"' 2>/dev/null)
        ;;
    esac
  done < "$outfile"
  rm -f "$outfile"

  if [[ -n "$turn_failed" ]]; then
    echo "$fail_msg" > "$ERR_FILE"
    return 1
  fi
  if [[ $rc -ne 0 ]]; then
    return 1
  fi
  return 0
}

emit() {  # emit <status> <session> <target> <lenses_csv> <report> <diagnostic>
  jq -nc \
    --arg status "$1" --arg session "$2" --arg target "$3" \
    --arg lenses "$4" --arg report "$5" --arg diag "$6" \
    '{status:$status, session_id:$session, target:$target,
      lenses:($lenses|split(",")|map(select(length>0))),
      report:$report, diagnostic:$diag}'
}

if ! run_codex "$PROMPT" "$SESSION"; then
  emit "BLOCKED" "$SESSION_ID" "$TARGET" "$LENSES" "" "codex exec invocation failed: $(cat "$ERR_FILE" 2>/dev/null)"
  exit 1
fi

if [[ -z "$REPORT" ]]; then
  emit "BLOCKED" "$SESSION_ID" "$TARGET" "$LENSES" "" "codex exec returned an empty review"
  exit 1
fi

emit "REVIEWED" "$SESSION_ID" "$TARGET" "$LENSES" "$REPORT" ""
exit 0
