#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
FAIL=0

require_text() {
  local file="$1" text="$2"
  if [[ ! -f "$ROOT/$file" ]] || ! grep -Fq -- "$text" "$ROOT/$file"; then
    echo "FAIL - $file missing: $text"
    FAIL=$((FAIL + 1))
  else
    echo "ok   - $file"
  fi
}

require_marker_target() {
  local file="$1" marker target
  marker="$(grep -F 'ADAPTER-PARITY:' "$ROOT/$file" | grep -F 'policy: docs/adapter-parity.md' || true)"
  target="${marker#*ADAPTER-PARITY: }"
  target="${target%%; policy:*}"
  if [[ -z "$marker" || "$target" == "$marker" || ! -f "$ROOT/$target" ]]; then
    echo "FAIL - $file parity target does not exist: $target"
    FAIL=$((FAIL + 1))
  else
    echo "ok   - $file target $target"
  fi
}

require_count() {
  local file="$1" text="$2" expected="$3" actual
  actual="$(grep -Fc -- "$text" "$ROOT/$file" || true)"
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL - $file expected $expected occurrences: $text (got $actual)"
    FAIL=$((FAIL + 1))
  else
    echo "ok   - $file has $expected occurrences"
  fi
}

require_text "commands/implement-plans.md" \
  "ADAPTER-PARITY: hermes/skills/cursor-coder/SKILL.md; policy: docs/adapter-parity.md"
require_text "hermes/skills/cursor-coder/SKILL.md" \
  "ADAPTER-PARITY: commands/implement-plans.md; policy: docs/adapter-parity.md"
require_text "agents/coder-delegator.md" \
  "ADAPTER-PARITY: hermes/scripts/dispatch.py; policy: docs/adapter-parity.md"
require_text "commands/review.md" \
  "ADAPTER-PARITY: hermes/skills/cursor-reviewer/SKILL.md; policy: docs/adapter-parity.md"
require_text "hermes/skills/cursor-reviewer/SKILL.md" \
  "ADAPTER-PARITY: commands/review.md; policy: docs/adapter-parity.md"
require_text "agents/reviewer-delegator.md" \
  "ADAPTER-PARITY: hermes/scripts/dispatch.py; policy: docs/adapter-parity.md"
require_text "AGENTS.md" "Classify the change as shared, Claude-specific, or Hermes-specific."
require_text "CLAUDE.md" "Classify the change as shared, Claude-specific, or Hermes-specific."
require_text "docs/adapter-parity.md" "Hermes coder uses run-scoped worktrees"
require_text "tests/e2e-smoke.md" 'if EMPTY="$(python3 "$ADAPTER" code'
require_text "tests/e2e-smoke.md" '--verify-cmd "false" --max-retries 3'
require_text "tests/e2e-smoke.md" 'if CLEANUP_RESULT="$(python3 "$ADAPTER" worktree --action remove'
require_text "tests/e2e-smoke.md" '"fsck", "--full", "--no-dangling"'
require_text "tests/e2e-smoke.md" '"git": module.git_control_manifest(repository, repository)'
require_count "tests/e2e-smoke.md" \
  'hermes config set plugins.entries.claude-subagents.settings.coder_model' 2
require_count "tests/e2e-smoke.md" \
  'hermes config set plugins.entries.claude-subagents.settings.reviewer_model' 2
require_count "tests/e2e-smoke.md" \
  'export HERMES_HOME="$SMOKE_ROOT/hermes-home"' 2
for mapped in \
  commands/implement-plans.md \
  hermes/skills/cursor-coder/SKILL.md \
  agents/coder-delegator.md \
  commands/review.md \
  hermes/skills/cursor-reviewer/SKILL.md \
  agents/reviewer-delegator.md; do
  require_marker_target "$mapped"
done

echo "---"
echo "FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
