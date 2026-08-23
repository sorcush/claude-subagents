#!/usr/bin/env bash
# Unit tests for scripts/probe.sh across all three harnesses.
# Run: bash tests/test-probe.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/probe.sh"

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
export CSC_CODEX_BIN="$HERE/mock-codex"
export CSC_CLAUDE_BIN="$HERE/mock-claude"

cat > "$TMP/reviewers.json" <<'EOF'
{"reviewers":[
  {"key":"c-cursor","label":"Cursor","harness":"cursor","model":"m-cursor"},
  {"key":"c-codex","label":"Codex","harness":"codex","model":"m-codex"},
  {"key":"c-claude","label":"Claude","harness":"claude","model":"m-claude"}]}
EOF
export CSC_REVIEWERS_JSON="$TMP/reviewers.json"

for k in c-cursor c-codex c-claude; do
  out=$(MOCK_RESULT=READY bash "$SCRIPT" --role reviewer --key "$k" 2>/dev/null)
  check "$k probe reports READY" "READY" "$(echo "$out" | jq -r '.status')"
  check "$k probe echoes key"    "$k"    "$(echo "$out" | jq -r '.key')"
  check "$k probe reason empty"  ""      "$(echo "$out" | jq -r '.reason')"
done

# The probe must send the model from the pool, not a hardcoded id.
log="$TMP/args.log"
MOCK_LOG="$log" MOCK_RESULT=READY bash "$SCRIPT" --role reviewer --key c-codex >/dev/null 2>&1
check "probe passes the pool's model" "1" "$(grep -c -- 'm-codex' "$log")"
rm -f "$log"

# A reply that is not exactly READY is a failure. These cases exist because a
# substring test would wrongly accept every one of them.
for bad in "not ready" "NOTREADY" "READY now" "I am READY to begin" "ALREADY"; do
  out=$(MOCK_RESULT="$bad" bash "$SCRIPT" --role reviewer --key c-codex 2>/dev/null)
  check "reply '$bad' is rejected" "FAILED" "$(echo "$out" | jq -r '.status')"
done

# Trailing punctuation and surrounding whitespace are tolerated; the word must
# still stand alone.
for ok in "READY" "READY."; do
  out=$(MOCK_RESULT="$ok" bash "$SCRIPT" --role reviewer --key c-codex 2>/dev/null)
  check "reply '$ok' is accepted" "READY" "$(echo "$out" | jq -r '.status')"
done

# A progress event that merely mentions READY must not count as the answer. The
# stream text is set explicitly: with the mock's default text this test would pass
# for the wrong reason and could never catch the bug it exists for.
# The check below is only meaningful if the progress event actually contains READY.
# Assert that precondition first: without it, a mock that ignores MOCK_STREAM_TEXT
# leaves no READY anywhere in the stream, and even a naive grep-the-whole-output
# implementation would pass. Verified: with the mock change stashed, the suite still
# reported 26/26.
raw=$(MOCK_STREAM=1 MOCK_STREAM_TEXT="READY" MOCK_RESULT="all done" \
      "$HERE/mock-codex" exec --json 2>/dev/null)
check "mock honours MOCK_STREAM_TEXT" "1" \
  "$([[ "$raw" == *'"text":"READY"'* ]] && echo 1 || echo 0)"

out=$(MOCK_STREAM=1 MOCK_STREAM_TEXT="READY" MOCK_RESULT="all done" \
      bash "$SCRIPT" --role reviewer --key c-codex 2>/dev/null)
check "READY in a progress event does not pass" "FAILED" "$(echo "$out" | jq -r '.status')"

# A CLI that fails outright is classified, and the real error is kept.
out=$(MOCK_FAIL_CLI=1 MOCK_STDERR="Authentication required" \
      bash "$SCRIPT" --role reviewer --key c-cursor 2>/dev/null)
check "auth failure detected"    "FAILED" "$(echo "$out" | jq -r '.status')"
check "auth failure classified"  "auth"   "$(echo "$out" | jq -r '.reason')"
check "diagnostic is preserved"  "1" \
  "$([[ "$(echo "$out" | jq -r '.diagnostic')" == *Authentication* ]] && echo 1 || echo 0)"

out=$(MOCK_FAIL_CLI=1 MOCK_STDERR="Workspace Trust Required" \
      bash "$SCRIPT" --role reviewer --key c-cursor 2>/dev/null)
check "trust failure classified" "trust" "$(echo "$out" | jq -r '.reason')"

# claude's failure branch is its own code, not shared with the other two harnesses,
# so it needs its own coverage. Without these, a dropped PROBE_REASON in claude.sh
# would pass the whole suite.
out=$(MOCK_FAIL_CLI=1 MOCK_STDERR="Authentication required" \
      bash "$SCRIPT" --role reviewer --key c-claude 2>/dev/null)
check "claude auth failure detected"   "FAILED" "$(echo "$out" | jq -r '.status')"
check "claude auth failure classified" "auth"   "$(echo "$out" | jq -r '.reason')"

out=$(MOCK_RESULT="NOTREADY" bash "$SCRIPT" --role reviewer --key c-claude 2>/dev/null)
check "claude rejects a READY substring" "FAILED" "$(echo "$out" | jq -r '.status')"

CSC_CURSOR_BIN=/no/such/binary bash "$SCRIPT" --role reviewer --key c-cursor >/dev/null 2>&1
check "missing binary exits non-zero" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"
out=$(CSC_CURSOR_BIN=/no/such/binary bash "$SCRIPT" --role reviewer --key c-cursor 2>/dev/null)
check "missing binary classified" "not-installed" "$(echo "$out" | jq -r '.reason')"

# A failed probe must exit non-zero so callers can stop.
MOCK_RESULT="nope" bash "$SCRIPT" --role reviewer --key c-codex >/dev/null 2>&1
check "failed probe exits non-zero" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"

bash "$SCRIPT" --role reviewer --key nosuch >/dev/null 2>&1
check "unknown key exits 2" "2" "$?"

# A bad model id must be reported as bad-model for EVERY harness — it is the only
# reason that sends the user to the pool file rather than to a login prompt.
for k in c-cursor c-claude; do
  out=$(MOCK_IS_ERROR=1 MOCK_RESULT="Cannot use this model: nope" \
        bash "$SCRIPT" --role reviewer --key "$k" 2>/dev/null)
  check "$k reports bad-model" "bad-model" "$(echo "$out" | jq -r '.reason')"
done
out=$(MOCK_TURN_FAILED=1 MOCK_STDERR="Cannot use this model: nope" \
      bash "$SCRIPT" --role reviewer --key c-codex 2>/dev/null)
check "c-codex reports bad-model" "bad-model" "$(echo "$out" | jq -r '.reason')"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
