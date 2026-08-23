#!/usr/bin/env bash
# Guards the core promise: the orchestrator never knows a model name.
# Every label and model id from the pool files must be absent from commands/
# and agents/. Menus are built at run time from pool.sh instead.
# Run: bash tests/test-no-model-names.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

values=$(jq -r '.reviewers[]|.label, .model' "$REPO/.claude-plugin/reviewers.json";
         jq -r '.coders[]|.label, .model'    "$REPO/.claude-plugin/coders.json")

check "pool values were read" "1" "$([[ -n "$values" ]] && echo 1 || echo 0)"

while IFS= read -r v; do
  [[ -n "$v" ]] || continue
  # Whole-string match on purpose. Matching word by word would flag legitimate
  # uses of single words like "Claude" or "Cursor" in ordinary prose.
  hits=$(grep -rlF -- "$v" "$REPO/commands" "$REPO/agents" 2>/dev/null | tr '\n' ' ')
  check "no orchestrator file names '$v'" "" "$hits"
done <<< "$values"

# The commands must ask the pool at run time rather than hardcoding a list.
check "review.md builds its menu from pool.sh" "1" \
  "$(grep -c 'pool.sh list reviewers' "$REPO/commands/review.md")"
check "implement-plans.md builds its menu from pool.sh" "1" \
  "$(grep -c 'pool.sh list coders' "$REPO/commands/implement-plans.md")"

# The old tool-specific commands must be gone.
for f in cursor-review.md codex-review.md cursor-implement-plans.md; do
  check "removed commands/$f" "1" "$([[ ! -e "$REPO/commands/$f" ]] && echo 1 || echo 0)"
done

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
