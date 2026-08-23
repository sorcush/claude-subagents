#!/usr/bin/env bash
# probe.sh — health-check one pool entry through its harness.
# Only one line of JSON reaches stdout; diagnostics go to stderr.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pool.sh
source "$SCRIPT_DIR/lib/pool.sh"
# shellcheck source=lib/timeout.sh
source "$SCRIPT_DIR/lib/timeout.sh"
# shellcheck source=lib/harness.sh
source "$SCRIPT_DIR/lib/harness.sh"

usage() { echo "usage: probe.sh --role <reviewer|coder> --key <key>" >&2; }

ROLE=""; KEY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift 2 ;;
    --key)  KEY="${2:-}";  shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$ROLE" && -n "$KEY" ]] || { usage; exit 2; }

ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT
PROBE_REASON=""

pool_load "$(pool_file_for "$ROLE")" "$ROLE"
pool_get "$KEY"
harness_load "$ENTRY_HARNESS"

emit() {  # emit <status> <reason> <diagnostic>
  jq -nc --arg status "$1" --arg role "$ROLE" --arg key "$KEY" \
         --arg reason "$2" --arg diag "$3" \
    '{status:$status, role:$role, key:$key, reason:$reason, diagnostic:$diag}'
}

if harness_probe "$ENTRY_MODEL"; then
  emit READY "" ""
  exit 0
fi

emit FAILED "${PROBE_REASON:-other}" "$(cat "$ERR_FILE" 2>/dev/null)"
exit 1
