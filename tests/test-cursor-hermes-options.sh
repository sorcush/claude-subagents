#!/usr/bin/env bash
# Cursor harness tests for opt-in sandboxing and bounded stream capture.
# Run: bash tests/test-cursor-hermes-options.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export CSC_CURSOR_BIN="$HERE/mock-cursor-agent"
# shellcheck source=../scripts/lib/timeout.sh
source "$HERE/../scripts/lib/timeout.sh"
# shellcheck source=../scripts/harness/cursor.sh
source "$HERE/../scripts/harness/cursor.sh"

ERR_FILE="$TMP/cursor.err"
SESSION_ID=""
RESULT=""

run_cursor() {
  local mode="$1"
  harness_run "$mode" cursor-model "$TMP" cursor-prompt ""
}

# Unset means legacy edit argv: no sandbox argument is added.
log="$TMP/edit-unset.log"
unset CSC_CURSOR_SANDBOX CSC_STREAM_MAX_BYTES
MOCK_LOG="$log" run_cursor edit >/dev/null 2>&1
check "unset sandbox leaves edit call successful" "0" "$?"
check "unset sandbox adds no argument" "0" \
  "$([[ "$(<"$log")" == *--sandbox* ]] && echo 1 || echo 0)"
check "unset sandbox preserves legacy edit argv" \
  "ARGS: -p --force --trust --approve-mcps --output-format stream-json --model cursor-model cursor-prompt " \
  "$(<"$log")"

# The sandbox is opt-in and applies only to edit mode.
log="$TMP/edit-enabled.log"
CSC_CURSOR_SANDBOX=enabled MOCK_LOG="$log" run_cursor edit >/dev/null 2>&1
check "enabled sandbox edit call succeeds" "0" "$?"
check "enabled sandbox is passed to edit mode" \
  "ARGS: -p --force --trust --approve-mcps --output-format stream-json --model cursor-model --sandbox enabled cursor-prompt " \
  "$(<"$log")"

log="$TMP/review-unset.log"
unset CSC_CURSOR_SANDBOX
MOCK_LOG="$log" run_cursor read-only >/dev/null 2>&1
review_legacy="$(<"$log")"
log="$TMP/review-enabled.log"
CSC_CURSOR_SANDBOX=enabled MOCK_LOG="$log" run_cursor read-only >/dev/null 2>&1
review_sandbox="$(<"$log")"
check "enabled sandbox leaves reviewer argv unchanged" "$review_legacy" "$review_sandbox"
check "reviewer keeps force trust approve-mcps and ask mode" \
  "ARGS: -p --force --trust --approve-mcps --output-format stream-json --model cursor-model --mode ask cursor-prompt " \
  "$review_sandbox"

# An unset stream limit must keep accepting output larger than the opt-in test limit.
unset CSC_CURSOR_SANDBOX CSC_STREAM_MAX_BYTES
MOCK_STREAM_BYTES=131072 run_cursor edit >/dev/null 2>&1
check "unset stream limit preserves legacy capture" "0" "$?"
check "legacy capture still returns terminal result" "done" "$RESULT"

# A real status 125 from Cursor is a legacy command failure when bounded capture
# is not selected. It must not enter the overflow-only branch or leak its capture.
legacy_capture_dir="$TMP/legacy-exit-125"
mkdir "$legacy_capture_dir"
legacy_stderr="$TMP/legacy-exit-125.stderr"
: > "$ERR_FILE"
(
  unset CSC_STREAM_MAX_BYTES
  TMPDIR="$legacy_capture_dir" MOCK_STDERR="legacy exit 125" MOCK_EXIT_CODE=125 \
    run_cursor edit
) >/dev/null 2>"$legacy_stderr"
legacy_rc=$?
check "legacy exit 125 is a structured harness failure" "1" "$legacy_rc"
check "legacy exit 125 preserves the tool diagnostic" "legacy exit 125" "$(<"$ERR_FILE")"
check "legacy exit 125 is not labeled stream overflow" "0" \
  "$([[ "$(<"$ERR_FILE")" == *"stream exceeded"* ]] && echo 1 || echo 0)"
check "legacy exit 125 does not expand an unset stream limit" "0" \
  "$([[ "$(<"$legacy_stderr")" == *"unbound variable"* ]] && echo 1 || echo 0)"
check "legacy exit 125 removes its temporary capture" "0" \
  "$(compgen -G "$legacy_capture_dir/*" >/dev/null && echo 1 || echo 0)"

# Configured limits reject non-positive and malformed values before launching Cursor.
for bad in 0 -1 abc 3+3; do
  : > "$ERR_FILE"
  CSC_STREAM_MAX_BYTES="$bad" run_cursor edit >/dev/null 2>&1
  check "stream limit '$bad' is rejected" "1" "$?"
  check "stream limit '$bad' explains positive integer requirement" "1" \
    "$([[ "$(<"$ERR_FILE")" == *positive\ integer* ]] && echo 1 || echo 0)"
done

# MOCK_STREAM_BYTES is an exact prefix size. A bound equal to that prefix plus
# the independently measured terminal result succeeds; one extra prefix byte fails.
terminal_result_file="$TMP/terminal-result.json"
MOCK_STREAM_BYTES= "$CSC_CURSOR_BIN" -p --force --trust --approve-mcps \
  --output-format stream-json --model cursor-model cursor-prompt > "$terminal_result_file"
terminal_result_bytes=$(wc -c < "$terminal_result_file")
boundary_prefix_bytes=4096
exact_stream_limit=$((terminal_result_bytes + boundary_prefix_bytes))

: > "$ERR_FILE"
CSC_STREAM_MAX_BYTES="$exact_stream_limit" MOCK_STREAM_BYTES="$boundary_prefix_bytes" \
  run_cursor edit >/dev/null 2>&1
check "stream exactly at configured limit succeeds" "0" "$?"
check "exact-limit stream returns terminal result" "done" "$RESULT"

: > "$ERR_FILE"
CSC_STREAM_MAX_BYTES="$exact_stream_limit" MOCK_STREAM_BYTES="$((boundary_prefix_bytes + 1))" \
  run_cursor edit >/dev/null 2>&1
check "stream one byte over configured limit fails" "1" "$?"
check "one-byte overflow reports configured limit" "1" \
  "$([[ "$(<"$ERR_FILE")" == *"stream exceeded $exact_stream_limit bytes"* ]] && echo 1 || echo 0)"

# A 64 KiB limit rejects a deterministic 128 KiB stream, reports the bound,
# and terminates descendants spawned by the Cursor process.
: > "$ERR_FILE"
mock_grandchild_file="$TMP/mock-grandchild.pid"
CSC_STREAM_MAX_BYTES=65536 MOCK_STREAM_BYTES=131072 \
  MOCK_GRANDCHILD_PID_FILE="$mock_grandchild_file" run_cursor edit >/dev/null 2>&1
check "128 KiB stream exceeds 64 KiB limit" "1" "$?"
check "overflow diagnostic names configured limit" "1" \
  "$([[ "$(<"$ERR_FILE")" == *"stream exceeded 65536 bytes"* ]] && echo 1 || echo 0)"
check "overflow returns no partial result" "" "$RESULT"
mock_grandchild=$(cat "$mock_grandchild_file" 2>/dev/null || echo "")
check "overflow mock grandchild pid was recorded" "1" \
  "$([[ -n "$mock_grandchild" ]] && echo 1 || echo 0)"
mock_grandchild_gone=0
if [[ -n "$mock_grandchild" ]]; then
  for _ in $(seq 1 50); do
    if ! ps -p "$mock_grandchild" -o stat= 2>/dev/null | grep -qv '^[[:space:]]*Z'; then
      mock_grandchild_gone=1
      break
    fi
    sleep 0.2
  done
fi
check "overflow kills mock grandchild" "1" "$mock_grandchild_gone"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
