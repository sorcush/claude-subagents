#!/usr/bin/env bash
# pool.sh — CLI over the pool library, so the orchestrator can build a menu.
#   pool.sh list <reviewers|coders>
#   pool.sh get  <reviewers|coders> <key>
# Only one line of JSON reaches stdout; diagnostics go to stderr.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pool.sh
source "$SCRIPT_DIR/lib/pool.sh"

usage() { echo "usage: pool.sh list <reviewers|coders> | pool.sh get <reviewers|coders> <key>" >&2; }

cmd="${1:-}"; role="${2:-}"
[[ -n "$cmd" && -n "$role" ]] || { usage; exit 2; }

pool_load "$(pool_file_for "$role")" "$role"

case "$cmd" in
  list)
    pool_list_json
    ;;
  get)
    key="${3:-}"; [[ -n "$key" ]] || { usage; exit 2; }
    pool_get "$key"
    jq -nc --arg key "$ENTRY_KEY" --arg label "$ENTRY_LABEL" \
           --arg harness "$ENTRY_HARNESS" --arg model "$ENTRY_MODEL" \
      '{key:$key, label:$label, harness:$harness, model:$model}'
    ;;
  *) usage; exit 2 ;;
esac
