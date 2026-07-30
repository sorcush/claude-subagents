#!/usr/bin/env bash
# Ensures the dynamic --model probes never regress to a hardcoded literal.
# sync-models.sh can't catch this (those spots are runtime-dynamic, not
# templated/marked), so this is a standalone grep-based guard.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$HERE/.."

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

no_literal_model() {  # no_literal_model <description> <file>
  # A --model flag followed directly by a quoted variable ($VAR or "$VAR")
  # is fine; a literal id string (composer-2.5, cursor-grok-4.5-high-fast)
  # right after --model is the regression this guards against.
  check "$1" "0" "$(grep -Ec -- '--model[[:space:]]+"?(composer-2\.5|cursor-grok-4\.5-high-fast)"?' "$2")"
}

no_literal_model "cc-delegate.sh has no hardcoded --model" "$REPO/scripts/cc-delegate.sh"
no_literal_model "cr-delegate.sh has no hardcoded --model" "$REPO/scripts/cr-delegate.sh"
no_literal_model "cursor-implement-plans.md healthcheck has no hardcoded --model" "$REPO/commands/cursor-implement-plans.md"
no_literal_model "cursor-review.md healthcheck has no hardcoded --model" "$REPO/commands/cursor-review.md"
no_literal_model "e2e-smoke.md probe has no hardcoded --model" "$REPO/tests/e2e-smoke.md"

no_literal_model_codex() {  # no_literal_model_codex <description> <file>
  check "$1" "0" "$(grep -Ec -- '--?m(odel)?[[:space:]]+"?gpt-5\.6-sol"?' "$2")"
}
no_literal_model_codex "cx-delegate.sh has no hardcoded -m/--model" "$REPO/scripts/cx-delegate.sh"
no_literal_model_codex "codex-review.md healthcheck has no hardcoded -m/--model" "$REPO/commands/codex-review.md"
no_literal_model_codex "e2e-smoke.md codex probe has no hardcoded -m/--model" "$REPO/tests/e2e-smoke.md"

check "cc-delegate.sh reads MODEL from jq" "1" "$(grep -c 'jq -r .*\.coder\.id' "$REPO/scripts/cc-delegate.sh")"
check "cr-delegate.sh reads MODEL from jq" "1" "$(grep -c 'jq -r .*\.reviewer\.id' "$REPO/scripts/cr-delegate.sh")"
check "cursor-implement-plans.md reads CODER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.coder\.id' "$REPO/commands/cursor-implement-plans.md")"
check "cursor-review.md reads REVIEWER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.reviewer\.id' "$REPO/commands/cursor-review.md")"
check "e2e-smoke.md reads REVIEWER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.reviewer\.id' "$REPO/tests/e2e-smoke.md")"
check "cx-delegate.sh reads MODEL from jq" "1" "$(grep -c 'jq -r .*\.codex_reviewer\.id' "$REPO/scripts/cx-delegate.sh")"
check "codex-review.md reads CODEX_REVIEWER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.codex_reviewer\.id' "$REPO/commands/codex-review.md")"
check "e2e-smoke.md reads CODEX_REVIEWER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.codex_reviewer\.id' "$REPO/tests/e2e-smoke.md")"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
