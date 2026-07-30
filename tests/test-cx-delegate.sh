#!/usr/bin/env bash
# Unit tests for cx-delegate.sh. Run: bash tests/test-cx-delegate.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/cx-delegate.sh"
MOCK="$HERE/mock-codex"
export CX_CODEX_BIN="$MOCK"   # script must honor this override

PASS=0
FAIL=0
check() {  # check <description> <expected> <actual>
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}
has() {  # has <description> <needle> <file>   (>=1 occurrence => 1)
  check "$1" "1" "$([ "$(grep -c -- "$2" "$3")" -ge 1 ] && echo 1 || echo 0)"
}

# Stub rubric dir with sentinels so we can assert prompt assembly.
RUBRICS=$(mktemp -d)
echo "SPEC_RUBRIC_SENTINEL"   > "$RUBRICS/spec-review.md"
echo "PLAN_RUBRIC_SENTINEL"   > "$RUBRICS/plan-review.md"
echo "BACKEND_LENS_SENTINEL"  > "$RUBRICS/lens-backend.md"
echo "FRONTEND_LENS_SENTINEL" > "$RUBRICS/lens-frontend.md"
echo "UI_LENS_SENTINEL"       > "$RUBRICS/lens-ui.md"
echo "OUTPUT_FORMAT_SENTINEL" > "$RUBRICS/_output-format.md"

# A real git repo for --doc-file, so default repo-root resolution has something to find.
# Resolved via `pwd -P` (physical path, not the raw mktemp -d path) so it matches
# `git rev-parse --show-toplevel`'s output on platforms where the tmp dir is itself a
# symlink (e.g. macOS /var -> /private/var) — git always resolves to the physical path.
REPO_DIR=$(cd "$(mktemp -d)" && pwd -P)
(cd "$REPO_DIR" && git init -q && git commit -q --allow-empty -m init)
doc="$REPO_DIR/spec.md"; echo "# Some Spec" > "$doc"

# --- model resolution from models.json ---
FIXTURE_MODELS=$(mktemp)
cat > "$FIXTURE_MODELS" <<'EOF'
{"coder": {"id": "fixture-coder-model", "label": "Fixture Coder"}, "reviewer": {"id": "fixture-reviewer-model", "label": "Fixture Reviewer"}, "codex_reviewer": {"id": "fixture-codex-model", "label": "Fixture Codex"}}
EOF

log=$(mktemp)
CX_MODELS_JSON="$FIXTURE_MODELS" MOCK_LOG="$log" bash "$SCRIPT" \
  --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" >/dev/null 2>&1
has "reads model id from CX_MODELS_JSON override" "fixture-codex-model" "$log"
rm -f "$log"

out=$(CX_MODELS_JSON=/no/such/file.json bash "$SCRIPT" --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null); rc=$?
check "missing models.json exits non-zero" "1" "$([ "$rc" -ne 0 ] && echo 1 || echo 0)"

BAD_MODELS=$(mktemp); echo '{"codex_reviewer": {}}' > "$BAD_MODELS"
out=$(CX_MODELS_JSON="$BAD_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null); rc=$?
check "models.json missing .codex_reviewer.id exits non-zero" "1" "$([ "$rc" -ne 0 ] && echo 1 || echo 0)"

rm -f "$BAD_MODELS"

# --- arg validation ---
out=$(bash "$SCRIPT" 2>/dev/null); rc=$?
check "no args exits 2" "2" "$rc"

bash "$SCRIPT" --target spec --rubric-dir "$RUBRICS" 2>/dev/null; rc=$?
check "missing --doc-file exits 2" "2" "$rc"

bash "$SCRIPT" --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null; rc=$?
check "missing --target exits 2" "2" "$rc"

bash "$SCRIPT" --target bogus --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null; rc=$?
check "invalid --target exits 2" "2" "$rc"

bash "$SCRIPT" --target spec --doc-file /no/such/file --rubric-dir "$RUBRICS" 2>/dev/null; rc=$?
check "unreadable --doc-file exits 2" "2" "$rc"

bash "$SCRIPT" --target spec --doc-file "$doc" --lenses bogus --rubric-dir "$RUBRICS" 2>/dev/null; rc=$?
check "invalid lens exits 2" "2" "$rc"

bash "$SCRIPT" --target spec --doc-file "$doc" --rubric-dir /no/such/dir 2>/dev/null; rc=$?
check "missing rubric dir exits 2" "2" "$rc"

bash "$SCRIPT" --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" --repo-root /no/such/dir 2>/dev/null; rc=$?
check "non-directory --repo-root exits 2" "2" "$rc"

# --- REVIEWED happy path ---
out=$(MOCK_SESSION=sess-9 MOCK_RESULT=myreview CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" \
  --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null)
check "status REVIEWED" "REVIEWED" "$(echo "$out" | jq -r .status)"
check "session captured from thread.started" "sess-9" "$(echo "$out" | jq -r .session_id)"
check "target captured" "spec" "$(echo "$out" | jq -r .target)"
check "report captured from last agent_message" "myreview" "$(echo "$out" | jq -r .report)"

# --- prompt assembly + invoked flags (mock logs full argv incl. the prompt) ---
log=$(mktemp)
MOCK_LOG="$log" CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" \
  --lenses backend,ui --rubric-dir "$RUBRICS" >/dev/null 2>&1
has  "prompt tells reviewer to read the doc" "$doc" "$log"
has  "prompt includes spec rubric" "SPEC_RUBRIC_SENTINEL" "$log"
has  "prompt includes selected backend lens" "BACKEND_LENS_SENTINEL" "$log"
has  "prompt includes selected ui lens" "UI_LENS_SENTINEL" "$log"
check "prompt omits unselected frontend lens" "0" "$(grep -c 'FRONTEND_LENS_SENTINEL' "$log")"
has  "prompt includes output format" "OUTPUT_FORMAT_SENTINEL" "$log"
has  "invokes exec subcommand" "exec" "$log"
has  "invokes read-only -s" "\-s read-only" "$log"
has  "invokes -C with a resolved repo root" "\-C $REPO_DIR" "$log"
has  "invokes --json" "\-\-json" "$log"
has  "invokes fixture-codex-model" "fixture-codex-model" "$log"
rm -f "$log"

# --- plan target reads the spec-file ---
spec=$(mktemp); echo "# Ref Spec" > "$spec"
log=$(mktemp)
MOCK_LOG="$log" CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target plan --doc-file "$doc" \
  --spec-file "$spec" --rubric-dir "$RUBRICS" >/dev/null 2>&1
has "plan: prompt references spec-file" "$spec" "$log"
has "plan: prompt includes plan rubric" "PLAN_RUBRIC_SENTINEL" "$log"
rm -f "$log" "$spec"

# --- lenses emitted as a JSON array ---
out=$(CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" \
  --lenses backend,frontend --rubric-dir "$RUBRICS" 2>/dev/null)
check "lenses[0] in json" "backend" "$(echo "$out" | jq -r '.lenses[0]')"
check "lenses[1] in json" "frontend" "$(echo "$out" | jq -r '.lenses[1]')"

# --- explicit --repo-root overrides git-root resolution ---
OTHER_ROOT=$(mktemp -d)
log=$(mktemp)
MOCK_LOG="$log" CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" \
  --rubric-dir "$RUBRICS" --repo-root "$OTHER_ROOT" >/dev/null 2>&1
has "explicit --repo-root used over git root" "\-C $OTHER_ROOT" "$log"
rm -f "$log"; rm -rf "$OTHER_ROOT"

# --- non-git doc dir falls back to the plugin's own repo root ---
NONGIT=$(mktemp -d)
nongit_doc="$NONGIT/spec.md"; echo "# doc" > "$nongit_doc"
log=$(mktemp)
MOCK_LOG="$log" CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$nongit_doc" \
  --rubric-dir "$RUBRICS" >/dev/null 2>&1
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
has "non-git doc falls back to plugin root" "\-C $PLUGIN_ROOT" "$log"
rm -f "$log"; rm -rf "$NONGIT"

# --- CLI failure -> BLOCKED ---
out=$(MOCK_FAIL_CLI=1 CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null); rc=$?
check "cli failure status BLOCKED" "BLOCKED" "$(echo "$out" | jq -r .status)"
check "cli failure exit 1" "1" "$rc"

out=$(MOCK_FAIL_CLI=1 MOCK_STDERR="boom: unauthenticated" CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" \
  --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null)
check "cli failure captures stderr in diagnostic" "1" \
  "$([ "$(echo "$out" | jq -r .diagnostic | grep -c 'boom: unauthenticated')" -ge 1 ] && echo 1 || echo 0)"

# --- turn.failed -> BLOCKED with the event's error message as diagnostic ---
out=$(MOCK_TURN_FAILED=1 CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null); rc=$?
check "turn.failed status BLOCKED" "BLOCKED" "$(echo "$out" | jq -r .status)"
check "turn.failed exit 1" "1" "$rc"
check "turn.failed diagnostic from event" "1" \
  "$([ "$(echo "$out" | jq -r .diagnostic | grep -c 'simulated provider error')" -ge 1 ] && echo 1 || echo 0)"
check "turn.failed captures session_id from thread.started" "mock-thread-1" "$(echo "$out" | jq -r .session_id)"

# --- empty report -> BLOCKED ---
out=$(MOCK_RESULT="" CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>/dev/null); rc=$?
check "empty report status BLOCKED" "BLOCKED" "$(echo "$out" | jq -r .status)"
check "empty report exit 1" "1" "$rc"

# --- progress event rendered to stderr, doesn't clobber the final agent_message ---
errf=$(mktemp)
out=$(MOCK_STREAM=1 MOCK_SESSION=sess-stream MOCK_RESULT=streamreview CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" \
  --target spec --doc-file "$doc" --rubric-dir "$RUBRICS" 2>"$errf")
check "stream: stdout single JSON line" "1" "$(echo "$out" | wc -l | tr -d ' ')"
check "stream: status REVIEWED" "REVIEWED" "$(echo "$out" | jq -r .status)"
check "stream: session from thread.started" "sess-stream" "$(echo "$out" | jq -r .session_id)"
check "stream: report is the last agent_message, not the pre-item" "streamreview" "$(echo "$out" | jq -r .report)"
has   "stream: pre-item progress rendered to stderr" "reasoning" "$errf"
rm -f "$errf"

# --- resume: --session maps to `codex exec resume`, no -C/-s on that invocation ---
log=$(mktemp)
MOCK_LOG="$log" CX_MODELS_JSON="$FIXTURE_MODELS" bash "$SCRIPT" --target spec --doc-file "$doc" \
  --session prev-123 --rubric-dir "$RUBRICS" >/dev/null 2>&1
has  "session uses resume subcommand" "resume prev-123" "$log"
check "resume invocation omits -C" "0" "$(grep -c -- '-C ' "$log")"
check "resume invocation omits -s" "0" "$(grep -c -- '-s ' "$log")"
rm -f "$log"

rm -rf "$RUBRICS" "$REPO_DIR"; rm -f "$doc" "$FIXTURE_MODELS"
echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
