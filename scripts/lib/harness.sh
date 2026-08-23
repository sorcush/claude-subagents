#!/usr/bin/env bash
# harness.sh (library) — load a harness file and classify its failures.
# Source this; do not execute it.

HARNESS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$HARNESS_LIB_DIR/../harness" && pwd)"

# harness_load <name> — sources scripts/harness/<name>.sh.
# The name was already checked against ^[a-z][a-z0-9-]*$ by pool_load, so it
# cannot escape this directory.
harness_load() {
  local name="$1" file="$HARNESS_DIR/$1.sh"
  [[ -r "$file" ]] || { echo "error: harness file not found: $file" >&2; exit 2; }
  # shellcheck source=/dev/null
  source "$file"
  local fn
  for fn in harness_probe harness_run harness_render; do
    declare -F "$fn" >/dev/null \
      || { echo "error: harness '$name' does not define $fn" >&2; exit 2; }
  done
}

# harness_is_ready <text> -> 0 only when the reply is exactly the word READY.
# A substring test would accept "NOTREADY", "READY later", or a progress event that
# merely mentions the word while the real answer was something else.
harness_is_ready() {
  local s="$1"
  s="$(printf '%s' "$s" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/[.!]*$//')"
  [[ "$s" == "READY" ]]
}

# harness_classify <error-text> -> one of auth|trust|not-installed|bad-model|other
# Used so a probe failure gives the right advice instead of always saying "log in".
harness_classify() {
  local t="$1"
  case "$t" in
    *"Authentication required"*|*"not logged in"*|*"Not logged in"*|*"Unauthorized"*|*"HTTP 401"*|*"status 401"*)
      echo auth ;;
    *"Workspace Trust"*|*"workspace is not trusted"*|*"Trust Required"*)
      echo trust ;;
    *"command not found"*|*"executable not found"*|*"is not recognized as"*)
      echo not-installed ;;
    *"Cannot use this model"*|*"unknown model"*|*"model not found"*|*"invalid model"*|*"Unsupported model"*)
      echo bad-model ;;
    *)
      echo other ;;
  esac
}
