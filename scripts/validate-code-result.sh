#!/usr/bin/env bash
# Validate the single-line result contract emitted by code-delegate.sh.
set -uo pipefail

usage() {
  echo "usage: validate-code-result.sh --exit-code <0|1> --result-file <path>" >&2
}

invalid() {
  echo "invalid code-delegate result: $1" >&2
  exit 2
}

EXIT_CODE=""
RESULT_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --exit-code)
      [[ $# -ge 2 ]] || invalid "--exit-code requires a value"
      EXIT_CODE="$2"; shift 2 ;;
    --result-file)
      [[ $# -ge 2 ]] || invalid "--result-file requires a value"
      RESULT_FILE="$2"; shift 2 ;;
    *) invalid "unknown argument: $1" ;;
  esac
done

[[ "$EXIT_CODE" == 0 || "$EXIT_CODE" == 1 ]] || invalid "exit code must be 0 or 1"
[[ -n "$RESULT_FILE" && -r "$RESULT_FILE" ]] || invalid "result file is unreadable"

nonempty_lines=$(awk 'NF { count++ } END { print count+0 }' "$RESULT_FILE")
[[ "$nonempty_lines" -eq 1 ]] || invalid "expected exactly one non-empty output line"
line=$(awk 'NF { print; exit }' "$RESULT_FILE")
jq -e 'type == "object"' <<<"$line" >/dev/null 2>&1 || invalid "output is not one JSON object"

if ! jq -e '
  has("status") and (.status|type=="string") and
  has("coder") and (.coder|type=="string") and
  has("session_id") and (.session_id|type=="string") and
  has("lifecycle_id") and (.lifecycle_id|type=="string") and
  has("attempts") and (.attempts|type=="number" and floor==. and .>=0) and
  has("verification_mode") and (.verification_mode=="commands" or .verification_mode=="none") and
  has("verification") and (.verification|type=="array") and
  (.verification | all(.[]; type=="object" and
    (.command|type=="string") and (.exit_code|type=="number" and floor==.) and
    (.timed_out|type=="boolean") and (.output|type=="string"))) and
  has("verified") and (.verified|type=="boolean") and
  has("changed") and (.changed|type=="boolean") and
  has("commit_id") and (.commit_id|type=="string") and
  has("files_changed") and (.files_changed|type=="array" and all(.[]; type=="string")) and
  has("worktree_clean") and (.worktree_clean|type=="boolean") and
  has("writer_stopped") and (.writer_stopped|type=="boolean") and
  has("result") and (.result|type=="string") and
  has("diagnostic") and (.diagnostic|type=="string")
' <<<"$line" >/dev/null 2>&1; then
  invalid "missing field or wrong field type"
fi

status=$(jq -r '.status' <<<"$line")
[[ ( "$status" == DONE && "$EXIT_CODE" == 0 ) \
   || ( "$status" == BLOCKED && "$EXIT_CODE" == 1 ) ]] \
  || invalid "status does not match process exit code"

if [[ "$status" == DONE ]]; then
  jq -e '
    (.session_id | length > 0) and
    (.writer_stopped == true) and
    (.worktree_clean == true) and
    ((.verification_mode == "none") or (.verified == true)) and
    ((.changed == true and (.commit_id | length > 0)) or
     (.changed == false and (.commit_id | length == 0)))
  ' <<<"$line" >/dev/null 2>&1 || invalid "DONE invariants are not satisfied"
fi

printf '%s\n' "$line"
[[ "$status" == DONE ]] && exit 0
exit 1
