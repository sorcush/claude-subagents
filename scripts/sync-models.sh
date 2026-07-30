#!/usr/bin/env bash
# sync-models.sh — regenerate every doc that names the coder/reviewer model
# from .claude-plugin/models.json. See docs/superpowers/specs/2026-07-18-*
# for the full design (templated fields vs marker-wrapped spans).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR/.."
MODELS_JSON="$REPO_DIR/.claude-plugin/models.json"
CHECK=0

usage() {
  echo "usage: sync-models.sh [--check] [--repo-dir <path>] [--models-json <path>]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)      CHECK=1; shift ;;
    --repo-dir)   REPO_DIR="${2:-}"; shift 2 ;;
    --models-json) MODELS_JSON="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

# --- validate models.json fully before touching any file ---
if [[ ! -r "$MODELS_JSON" ]]; then
  echo "error: models.json missing or unreadable: $MODELS_JSON" >&2; exit 2
fi
if ! jq -e . "$MODELS_JSON" >/dev/null 2>&1; then
  echo "error: models.json is not valid JSON: $MODELS_JSON" >&2; exit 2
fi

CODER_ID="$(jq -r '.coder.id // empty' "$MODELS_JSON")"
CODER_LABEL="$(jq -r '.coder.label // empty' "$MODELS_JSON")"
REVIEWER_ID="$(jq -r '.reviewer.id // empty' "$MODELS_JSON")"
REVIEWER_LABEL="$(jq -r '.reviewer.label // empty' "$MODELS_JSON")"
CODEX_REVIEWER_ID="$(jq -r '.codex_reviewer.id // empty' "$MODELS_JSON")"
CODEX_REVIEWER_LABEL="$(jq -r '.codex_reviewer.label // empty' "$MODELS_JSON")"

for name_val in "coder.id:$CODER_ID" "coder.label:$CODER_LABEL" "reviewer.id:$REVIEWER_ID" "reviewer.label:$REVIEWER_LABEL" "codex_reviewer.id:$CODEX_REVIEWER_ID" "codex_reviewer.label:$CODEX_REVIEWER_LABEL"; do
  name="${name_val%%:*}"
  val="${name_val#*:}"
  if [[ -z "$val" ]]; then
    echo "error: models.json missing or empty .$name" >&2; exit 2
  fi
done

ID_RE='^[A-Za-z0-9._+-]+$'
if [[ ! "$CODER_ID" =~ $ID_RE ]]; then
  echo "error: .coder.id '$CODER_ID' violates the allowed charset $ID_RE" >&2; exit 2
fi
if [[ ! "$REVIEWER_ID" =~ $ID_RE ]]; then
  echo "error: .reviewer.id '$REVIEWER_ID' violates the allowed charset $ID_RE" >&2; exit 2
fi
if [[ ! "$CODEX_REVIEWER_ID" =~ $ID_RE ]]; then
  echo "error: .codex_reviewer.id '$CODEX_REVIEWER_ID' violates the allowed charset $ID_RE" >&2; exit 2
fi

check_label_charset() {  # check_label_charset <field-name> <value>
  case "$2" in
    *:*|*'"'*|*'`'*)
      echo "error: .$1 '$2' contains a forbidden character (: \" \`)" >&2; exit 2 ;;
  esac
  if [[ "$2" == *$'\n'* ]]; then
    echo "error: .$1 contains a newline" >&2; exit 2
  fi
  if LC_ALL=C printf '%s' "$2" | grep -q '[^ -~]'; then
    echo "error: .$1 '$2' contains a non-ASCII or non-printable character" >&2; exit 2
  fi
}
check_label_charset "coder.label" "$CODER_LABEL"
check_label_charset "reviewer.label" "$REVIEWER_LABEL"
check_label_charset "codex_reviewer.label" "$CODEX_REVIEWER_LABEL"

STALE_FILES=()
TOUCHED_FILES=()

# mktemp_for <file> — sets $TMP_OUT to a temp path in the SAME directory as
# <file> (so the later mv is a same-filesystem atomic rename, not a
# cross-fs copy). Called as a plain statement, never via $(...), because it
# must be able to exit the whole script on failure, not just a subshell.
# Copies <file>'s existing permission bits onto the temp file — mktemp's
# default mode (0600) would otherwise silently strip the +x bit off every
# executable file this script touches (e.g. the cc-/cr-/cx-delegate.sh
# scripts) once the mv lands.
mktemp_for() {
  local file="$1"
  if ! TMP_OUT="$(mktemp "$(dirname "$file")/.sync-models.XXXXXX" 2>/dev/null)"; then
    echo "error: could not create a temp file next to $file (directory not writable?)" >&2
    echo "reached: ${TOUCHED_FILES[*]:-<none>}" >&2
    echo "not reached: $file and everything after it in the file list" >&2
    exit 1
  fi
  if [[ -f "$file" ]]; then
    chmod --reference="$file" "$TMP_OUT" 2>/dev/null || chmod "$(stat -f '%Mp%Lp' "$file" 2>/dev/null || stat -c '%a' "$file" 2>/dev/null)" "$TMP_OUT" 2>/dev/null || true
  fi
}

# atomic_write <file> <tmp> — check mode: diff against tmp, cleanup, record
# staleness. Write mode: mv with failure handling (cleanup tmp on failure,
# report reached/not-reached, exit non-zero; record success in TOUCHED_FILES).
atomic_write() {
  local file="$1" tmp="$2"
  if [[ "$CHECK" -eq 1 ]]; then
    if ! diff -q "$file" "$tmp" >/dev/null 2>&1; then STALE_FILES+=("$file"); fi
    rm -f "$tmp"
    return 0
  fi
  if ! mv "$tmp" "$file" 2>/dev/null; then
    rm -f "$tmp"
    echo "error: failed to write $file" >&2
    echo "reached: ${TOUCHED_FILES[*]:-<none>}" >&2
    echo "not reached: $file and everything after it in the file list" >&2
    exit 1
  fi
  TOUCHED_FILES+=("$file")
}

# regen_json_field_arg <file> <jq-path-expr-using-$d> <value>
regen_json_field_arg() {
  local file="$1" expr="$2" value="$3"
  [[ ! -f "$file" ]] && { echo "error: expected file missing: $file" >&2; exit 2; }
  mktemp_for "$file"; local tmp="$TMP_OUT"
  if ! jq --arg d "$value" "$expr" "$file" > "$tmp"; then
    rm -f "$tmp"; echo "error: jq failed regenerating $file" >&2; exit 2
  fi
  atomic_write "$file" "$tmp"
}

# regen_frontmatter_description <file> <new-description-text>
regen_frontmatter_description() {
  local file="$1" new="$2"
  [[ ! -f "$file" ]] && { echo "error: expected file missing: $file" >&2; exit 2; }
  mktemp_for "$file"; local tmp="$TMP_OUT"
  NEW_LINE="description: $new" awk '
    BEGIN { done = 0 }
    {
      if (!done && $0 ~ /^description: /) {
        print ENVIRON["NEW_LINE"]
        done = 1
      } else {
        print
      }
    }
  ' "$file" > "$tmp"
  atomic_write "$file" "$tmp"
}

# regen_markers <file> <which: coder|reviewer|codex_reviewer>
regen_markers() {
  local file="$1" which="$2" value tag
  [[ ! -f "$file" ]] && { echo "error: expected file missing: $file" >&2; exit 2; }
  case "$which" in
    coder)          value="$CODER_LABEL" ;;
    reviewer)       value="$REVIEWER_LABEL" ;;
    codex_reviewer) value="$CODEX_REVIEWER_LABEL" ;;
    *) echo "error: regen_markers: unknown role '$which'" >&2; exit 2 ;;
  esac
  tag="model:$which:label"
  if ! grep -q -- "<!-- $tag -->" "$file"; then
    echo "error: required marker <!-- $tag --> missing from $file" >&2
    exit 1
  fi
  mktemp_for "$file"; local tmp="$TMP_OUT"
  NEW_VALUE="$value" TAG="$tag" perl -0777 -pe '
    my $val = $ENV{NEW_VALUE};
    s/(<!-- \Q$ENV{TAG}\E -->).*?(<!-- \/\Q$ENV{TAG}\E -->)/$1 . $val . $2/ges;
  ' "$file" > "$tmp"
  atomic_write "$file" "$tmp"
}

# --- plugin.json / marketplace.json descriptions (keywords is never touched) ---
PLUGIN_DESC="Delegate implementation to Cursor's ${CODER_LABEL}, and independent design-spec/plan review to ${REVIEWER_LABEL} via cursor-agent or ${CODEX_REVIEWER_LABEL} via the Codex CLI, while Claude/Opus plans, decides, and reviews."
MARKETPLACE_DESC="Delegation subagents: ${CODER_LABEL} for implementation, and independent design/plan review via ${REVIEWER_LABEL} (cursor-agent) or ${CODEX_REVIEWER_LABEL} (Codex CLI)."

regen_json_field_arg "$REPO_DIR/.claude-plugin/plugin.json" '.description = $d' "$PLUGIN_DESC"
regen_json_field_arg "$REPO_DIR/.claude-plugin/marketplace.json" '.plugins[0].description = $d' "$MARKETPLACE_DESC"

# --- frontmatter description: lines ---
CODER_AGENT_DESC="Delegates a single implementation task to Cursor's ${CODER_LABEL} model via cursor-agent, verifies it, and reports back. Use as the implementer in subagent-driven development when delegating coding to Composer. Does not design, review, or write code itself — it delegates and verifies."
REVIEWER_AGENT_DESC="Delegates an independent design-spec or implementation-plan review to ${REVIEWER_LABEL} via cursor-agent in read-only mode, and relays the report. Use as the reviewer when an independent, unbiased review of a spec or plan is needed. Does not author, judge, or edit — it delegates and relays."
CODER_CMD_DESC="Implement a written plan by delegating each task to Cursor's ${CODER_LABEL} (via the cursor-coder-delegator subagent), while Opus reviews. Usage: /cursor-implement-plans <plan-path>"
REVIEWER_CMD_DESC="Get an independent ${REVIEWER_LABEL} review of a design spec or implementation plan, removing the bias of self-review. Usage: /cursor-review <spec|plan> <doc-path> [spec-path]"
CODEX_REVIEWER_AGENT_DESC="Delegates an independent design-spec or implementation-plan review to ${CODEX_REVIEWER_LABEL} via the Codex CLI (\`codex exec\`) in a read-only sandbox, and relays the report. Use as the reviewer when an independent, unbiased review of a spec or plan is needed from a different vendor/model than the Cursor/Grok reviewer. Does not author, judge, or edit — it delegates and relays."
CODEX_REVIEWER_CMD_DESC="Get an independent ${CODEX_REVIEWER_LABEL} review of a design spec or implementation plan, removing the bias of self-review. Usage: /codex-review <spec|plan> <doc-path> [spec-path]"

regen_frontmatter_description "$REPO_DIR/agents/cursor-coder-delegator.md" "$CODER_AGENT_DESC"
regen_frontmatter_description "$REPO_DIR/agents/cursor-reviewer-delegator.md" "$REVIEWER_AGENT_DESC"
regen_frontmatter_description "$REPO_DIR/commands/cursor-implement-plans.md" "$CODER_CMD_DESC"
regen_frontmatter_description "$REPO_DIR/commands/cursor-review.md" "$REVIEWER_CMD_DESC"
regen_frontmatter_description "$REPO_DIR/agents/codex-reviewer-delegator.md" "$CODEX_REVIEWER_AGENT_DESC"
regen_frontmatter_description "$REPO_DIR/commands/codex-review.md" "$CODEX_REVIEWER_CMD_DESC"

# --- marker-wrapped spans ---
# file:tag pairs this repo requires. Extend this list if a new file gains a
# marker (see the spec's Rollout section for the authoritative inventory).
MARKER_TARGETS=(
  "$REPO_DIR/README.md:coder"
  "$REPO_DIR/README.md:reviewer"
  "$REPO_DIR/README.md:codex_reviewer"
  "$REPO_DIR/agents/cursor-coder-delegator.md:coder"
  "$REPO_DIR/agents/cursor-reviewer-delegator.md:reviewer"
  "$REPO_DIR/agents/codex-reviewer-delegator.md:codex_reviewer"
  "$REPO_DIR/commands/cursor-implement-plans.md:coder"
  "$REPO_DIR/commands/cursor-review.md:reviewer"
  "$REPO_DIR/commands/codex-review.md:codex_reviewer"
  "$REPO_DIR/scripts/cc-delegate.sh:coder"
  "$REPO_DIR/scripts/cr-delegate.sh:reviewer"
  "$REPO_DIR/scripts/cx-delegate.sh:codex_reviewer"
  "$REPO_DIR/tests/e2e-smoke.md:reviewer"
  "$REPO_DIR/tests/e2e-smoke.md:codex_reviewer"
)

for entry in "${MARKER_TARGETS[@]}"; do
  file="${entry%%:*}"
  which="${entry##*:}"
  regen_markers "$file" "$which"
done

if [[ "$CHECK" -eq 1 && ${#STALE_FILES[@]} -gt 0 ]]; then
  echo "stale (would change on sync):" >&2
  printf '  %s\n' "${STALE_FILES[@]}" >&2
  exit 1
fi

exit 0
