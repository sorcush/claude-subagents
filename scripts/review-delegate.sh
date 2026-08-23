#!/usr/bin/env bash
# review-delegate.sh — delegate ONE design-spec or plan review to any reviewer
# from the pool, read-only, and return its report.
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
  echo "usage: review-delegate.sh --reviewer <key> --target spec|plan --doc-file <path>" >&2
  echo "       [--spec-file <path>] [--lenses backend,frontend,ui] [--rubric-dir <path>] [--session <id>]" >&2
}

REVIEWER=""; TARGET=""; DOC_FILE=""; SPEC_FILE=""; LENSES=""; SESSION=""
RUBRIC_DIR="$SCRIPT_DIR/../rubrics"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reviewer)   REVIEWER="${2:-}";   shift 2 ;;
    --target)     TARGET="${2:-}";     shift 2 ;;
    --doc-file)   DOC_FILE="${2:-}";   shift 2 ;;
    --spec-file)  SPEC_FILE="${2:-}";  shift 2 ;;
    --lenses)     LENSES="${2:-}";     shift 2 ;;
    --rubric-dir) RUBRIC_DIR="${2:-}"; shift 2 ;;
    --session)    SESSION="${2:-}";    shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$REVIEWER" ]] || { echo "error: --reviewer is required" >&2; usage; exit 2; }
[[ "$TARGET" == "spec" || "$TARGET" == "plan" ]] \
  || { echo "error: --target must be 'spec' or 'plan'" >&2; usage; exit 2; }
[[ -n "$DOC_FILE" && -r "$DOC_FILE" ]] \
  || { echo "error: --doc-file missing or unreadable" >&2; usage; exit 2; }
[[ -z "$SPEC_FILE" || -r "$SPEC_FILE" ]] \
  || { echo "error: --spec-file unreadable: $SPEC_FILE" >&2; exit 2; }

TARGET_RUBRIC="$RUBRIC_DIR/${TARGET}-review.md"
OUTPUT_FMT="$RUBRIC_DIR/_output-format.md"
for f in "$TARGET_RUBRIC" "$OUTPUT_FMT"; do
  [[ -r "$f" ]] || { echo "error: required rubric file missing: $f" >&2; exit 2; }
done

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
    [[ -r "$lf" ]] || { echo "error: lens rubric file missing: $lf" >&2; exit 2; }
    LENS_FILES+=("$lf")
  done
fi

pool_load "$(pool_file_for reviewer)" reviewer
pool_get "$REVIEWER"
harness_load "$ENTRY_HARNESS"

# Ground the reviewer in the document's own repository when it has one.
resolve_root() {
  local d; d="$(cd "$(dirname "$DOC_FILE")" && pwd)"
  git -C "$d" rev-parse --show-toplevel 2>/dev/null || echo "$d"
}
REPO_ROOT="$(resolve_root)"

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
  for lf in "${LENS_FILES[@]:-}"; do
    [[ -n "$lf" ]] || continue
    echo
    cat "$lf"
  done
  echo
  echo "Produce your review in EXACTLY the following format:"
  echo
  cat "$OUTPUT_FMT"
}

ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT
SESSION_ID=""
RESULT=""

emit() {  # emit <status> <session> <report> <diagnostic>
  jq -nc --arg status "$1" --arg reviewer "$REVIEWER" --arg session "$2" \
         --arg target "$TARGET" --arg lenses "$LENSES" \
         --arg report "$3" --arg diag "$4" \
    '{status:$status, reviewer:$reviewer, session_id:$session, target:$target,
      lenses:($lenses|split(",")|map(select(length>0))),
      report:$report, diagnostic:$diag}'
}

# harness_run is a PLAIN STATEMENT: it returns values in shell variables, which
# a $(...) or a pipeline would discard in a subshell.
if ! harness_run "read-only" "$ENTRY_MODEL" "$REPO_ROOT" "$(assemble_prompt)" "$SESSION"; then
  emit BLOCKED "$SESSION_ID" "" "reviewer invocation failed: $(cat "$ERR_FILE" 2>/dev/null)"
  exit 1
fi

if [[ -z "$RESULT" ]]; then
  emit BLOCKED "$SESSION_ID" "" "reviewer returned an empty review"
  exit 1
fi

emit REVIEWED "$SESSION_ID" "$RESULT" ""
exit 0
