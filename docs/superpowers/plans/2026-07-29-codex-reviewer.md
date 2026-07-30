# codex-reviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third, independent reviewer to this plugin — design-spec and plan review delegated to GPT-5.6 Sol via the Codex CLI (`codex exec`), parallel to the existing Cursor/Grok reviewer, with the controller asking the user which reviewer to use at self-review gates.

**Architecture:** Mirror the existing `cr-delegate.sh` / `cursor-reviewer-delegator` / `/cursor-review` trio exactly, swapping `cursor-agent --mode ask` for `codex exec -s read-only -C <repo-root>`. Same rubrics, same JSON envelope contract, same Haiku relay-only subagent. `models.json` gains a third role (`codex_reviewer`); `sync-models.sh`'s marker regeneration becomes a three-way dispatch instead of binary.

**Tech Stack:** bash, jq, the `codex` CLI (`codex exec`), Claude Code agents/commands/skills.

## Global Constraints

- Model id: `gpt-5.6-sol`, label: `GPT-5.6 Sol` (verified working against the installed `codex-cli 0.144.1`).
- Sandbox: always `-s read-only` on the initial `codex exec` invocation (never `workspace-write` or `danger-full-access`, never `--dangerously-bypass-approvals-and-sandbox` / `--dangerously-bypass-hook-trust`).
- stdin: always redirect `< /dev/null` on every `codex exec` invocation (avoids the "Reading additional input from stdin..." wait).
- Only the delegate script's final status JSON goes to stdout; all progress/diagnostics go to stderr — same discipline as `cr-delegate.sh`.
- Exit codes: `0` REVIEWED, `1` BLOCKED, `2` argument/validation failure — matching `cr-delegate.sh` exactly.
- `rubrics/` is never modified — both reviewers share the existing files verbatim.
- The existing `reviewer` key/behavior in `models.json` / `cr-delegate.sh` / `cursor-reviewer-delegator.md` / `commands/cursor-review.md` is never changed by this work, only added alongside.
- New file naming: `cx-delegate.sh` (script), `codex-reviewer-delegator` (agent), `/codex-review` (command) — mirrors `cr-*` / `cursor-reviewer-delegator` / `/cursor-review`.

---

### Task 1: `cx-delegate.sh` delegate script + mock + unit tests

**Files:**
- Create: `tests/mock-codex`
- Create: `scripts/cx-delegate.sh`
- Create: `tests/test-cx-delegate.sh`

**Interfaces:**
- Produces: `scripts/cx-delegate.sh` CLI — `--target spec|plan --doc-file <path> [--spec-file <path>] [--lenses backend,frontend,ui] [--rubric-dir <path>] [--session <id>] [--repo-root <path>]`, env overrides `CX_CODEX_BIN` (default `codex`) and `CX_MODELS_JSON` (default `$SCRIPT_DIR/../.claude-plugin/models.json`), reads `.codex_reviewer.id` from that JSON. Stdout: one JSON line `{"status":"REVIEWED"|"BLOCKED","session_id":...,"target":...,"lenses":[...],"report":...,"diagnostic":...}`. Exit `0`/`1`/`2`.
- Consumes: `rubrics/spec-review.md`, `rubrics/plan-review.md`, `rubrics/lens-{backend,frontend,ui}.md`, `rubrics/_output-format.md` (all pre-existing, unmodified).

- [ ] **Step 1: Create the mock `codex` binary**

Write `tests/mock-codex`:

```bash
#!/usr/bin/env bash
# Mock of `codex` for cx-delegate.sh unit tests. Mimics verified real behavior of
# `codex exec --json` / `codex exec resume --json`:
#   - prints newline-delimited JSON events to stdout
#   - exits 0 on success, 1 on simulated failure
# Controls (env vars):
#   MOCK_FAIL_CLI=1      -> print a plain-text error to stderr, exit 1, no stdout JSON
#   MOCK_STDERR=<text>   -> also write this text to stderr
#   MOCK_TURN_FAILED=1   -> emit thread.started + a turn.failed event, exit 1
#   MOCK_SESSION=<id>    -> thread_id to emit (default mock-thread-1)
#   MOCK_RESULT=<text>   -> agent_message text to emit (default "done"; empty stays empty)
#   MOCK_STREAM=1        -> emit a non-agent_message item.completed event before the final one
#   MOCK_LOG=<path>      -> append "ARGS: $*" (all argv, incl. the prompt) to this file
set -euo pipefail

if [[ -n "${MOCK_LOG:-}" ]]; then echo "ARGS: $*" >> "$MOCK_LOG"; fi
if [[ -n "${MOCK_STDERR:-}" ]]; then echo "$MOCK_STDERR" >&2; fi

session="${MOCK_SESSION:-mock-thread-1}"
result="${MOCK_RESULT-done}"

if [[ "${MOCK_FAIL_CLI:-0}" == "1" ]]; then
  echo "codex: simulated CLI failure" >&2
  exit 1
fi

printf '{"type":"thread.started","thread_id":"%s"}\n' "$session"
printf '{"type":"turn.started"}\n'

if [[ "${MOCK_TURN_FAILED:-0}" == "1" ]]; then
  printf '{"type":"turn.failed","error":{"message":"simulated provider error"}}\n'
  exit 1
fi

if [[ "${MOCK_STREAM:-0}" == "1" ]]; then
  printf '{"type":"item.completed","item":{"id":"item_pre","type":"reasoning","text":"exploring README.md"}}\n'
fi

printf '{"type":"item.completed","item":{"id":"item_final","type":"agent_message","text":"%s"}}\n' "$result"
printf '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
exit 0
```

Make it executable:
```bash
chmod +x tests/mock-codex
```

- [ ] **Step 2: Write the failing test file**

Write `tests/test-cx-delegate.sh`:

```bash
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
REPO_DIR=$(mktemp -d)
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
```

- [ ] **Step 3: Run the test file to confirm it fails**

```bash
chmod +x tests/test-cx-delegate.sh
bash tests/test-cx-delegate.sh
```
Expected: fails immediately — `scripts/cx-delegate.sh: No such file or directory` (or similar), since the script doesn't exist yet.

- [ ] **Step 4: Implement `scripts/cx-delegate.sh`**

```bash
#!/usr/bin/env bash
# cx-delegate.sh — delegate ONE design-spec or plan review to <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->
# via the Codex CLI (`codex exec`) in a read-only sandbox, and return its report.
# Output discipline: ONLY the final STATUS JSON goes to stdout; diagnostics/progress to stderr.
set -uo pipefail

CODEX_BIN="${CX_CODEX_BIN:-codex}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_JSON="${CX_MODELS_JSON:-$SCRIPT_DIR/../.claude-plugin/models.json}"
MODEL="$(jq -r '.codex_reviewer.id // empty' "$MODELS_JSON" 2>/dev/null)"
if [[ -z "$MODEL" ]]; then
  echo "error: could not read .codex_reviewer.id from $MODELS_JSON" >&2
  exit 2
fi

usage() {
  echo "usage: cx-delegate.sh --target spec|plan --doc-file <path> [--spec-file <path>] [--lenses backend,frontend,ui] [--rubric-dir <path>] [--session <id>] [--repo-root <path>]" >&2
}

TARGET=""
DOC_FILE=""
SPEC_FILE=""
LENSES=""
RUBRIC_DIR="$SCRIPT_DIR/../rubrics"
SESSION=""
REPO_ROOT_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)     TARGET="${2:-}"; shift 2 ;;
    --doc-file)   DOC_FILE="${2:-}"; shift 2 ;;
    --spec-file)  SPEC_FILE="${2:-}"; shift 2 ;;
    --lenses)     LENSES="${2:-}"; shift 2 ;;
    --rubric-dir) RUBRIC_DIR="${2:-}"; shift 2 ;;
    --session)    SESSION="${2:-}"; shift 2 ;;
    --repo-root)  REPO_ROOT_ARG="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

# --- arg validation ---
if [[ "$TARGET" != "spec" && "$TARGET" != "plan" ]]; then
  echo "error: --target must be 'spec' or 'plan'" >&2; usage; exit 2
fi
if [[ -z "$DOC_FILE" || ! -r "$DOC_FILE" ]]; then
  echo "error: --doc-file missing or unreadable" >&2; usage; exit 2
fi
if [[ -n "$SPEC_FILE" && ! -r "$SPEC_FILE" ]]; then
  echo "error: --spec-file given but unreadable: $SPEC_FILE" >&2; usage; exit 2
fi
if [[ -n "$REPO_ROOT_ARG" && ! -d "$REPO_ROOT_ARG" ]]; then
  echo "error: --repo-root is not a directory: $REPO_ROOT_ARG" >&2; exit 2
fi

# Map target -> rubric file; both target rubric and the output format are required.
TARGET_RUBRIC="$RUBRIC_DIR/${TARGET}-review.md"
OUTPUT_FMT="$RUBRIC_DIR/_output-format.md"
for f in "$TARGET_RUBRIC" "$OUTPUT_FMT"; do
  if [[ ! -r "$f" ]]; then
    echo "error: required rubric file missing: $f" >&2; exit 2
  fi
done

# Parse + validate lenses (spec reviews). Unknown lens or missing file -> exit 2.
LENS_FILES=()
if [[ -n "$LENSES" ]]; then
  IFS=',' read -ra _lenses <<< "$LENSES"
  for lens in "${_lenses[@]}"; do
    [[ -z "$lens" ]] && continue
    case "$lens" in
      backend|frontend|ui) ;;
      *) echo "error: unknown lens '$lens' (allowed: backend, frontend, ui)" >&2; exit 2 ;;
    esac
    lf="$RUBRIC_DIR/lens-${lens}.md"
    if [[ ! -r "$lf" ]]; then
      echo "error: lens rubric file missing: $lf" >&2; exit 2
    fi
    LENS_FILES+=("$lf")
  done
fi

# --- resolve the Codex working root (grounds "explore the existing repo" in a real dir) ---
resolve_repo_root() {
  if [[ -n "$REPO_ROOT_ARG" ]]; then
    REPO_ROOT="$(cd "$REPO_ROOT_ARG" && pwd)"
    return
  fi
  local doc_dir git_root
  doc_dir="$(cd "$(dirname "$DOC_FILE")" && pwd)"
  if git_root=$(git -C "$doc_dir" rev-parse --show-toplevel 2>/dev/null); then
    REPO_ROOT="$git_root"
  else
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  fi
}
resolve_repo_root

# --- assemble the review prompt (inlines rubrics; points the reviewer at the doc) ---
assemble_prompt() {
  cat <<EOF
You are an independent, senior software reviewer. You did NOT author the document
under review — your job is to challenge it rigorously and surface problems before
implementation. You have READ-ONLY access to this repository: explore the existing
code as needed to judge how the proposed work fits the current codebase and whether
it risks breaking existing behavior. Do not attempt to modify any files.

The document under review is at: $DOC_FILE
Read it in full before reviewing.
EOF
  if [[ -n "$SPEC_FILE" ]]; then
    echo "The reference spec this document must satisfy is at: $SPEC_FILE"
    echo "Read it too and check the document against it."
  fi
  echo
  echo "Apply the following review rubric:"
  echo
  cat "$TARGET_RUBRIC"
  if [[ ${#LENS_FILES[@]} -gt 0 ]]; then
    for lf in "${LENS_FILES[@]}"; do
      echo
      cat "$lf"
    done
  fi
  echo
  echo "Produce your review in EXACTLY the following format:"
  echo
  cat "$OUTPUT_FMT"
}

PROMPT="$(assemble_prompt)"

# --- run codex once (read-only), render progress to stderr, capture session + report ---
ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT

render_event() {  # render_event <json-line> — best-effort progress; unknown types ignored
  local line="$1" type
  type=$(echo "$line" | jq -r '.type // ""' 2>/dev/null) || return 0
  case "$type" in
    item.completed)
      local itype text
      itype=$(echo "$line" | jq -r '.item.type // ""' 2>/dev/null)
      [[ "$itype" == "agent_message" ]] && return 0
      text=$(echo "$line" | jq -r '.item.text // .item.command // ""' 2>/dev/null)
      echo "  -> ${itype}${text:+ ($text)}" >&2
      ;;
  esac
}

SESSION_ID=""
REPORT=""

run_codex() {  # run_codex <prompt> [session_id] — sets SESSION_ID / REPORT, returns 1 on failure
  local prompt="$1" sess="${2:-}"
  local rc outfile turn_failed="" fail_msg=""
  : > "$ERR_FILE"
  outfile=$(mktemp)
  local -a cmd
  if [[ -n "$sess" ]]; then
    cmd=("$CODEX_BIN" exec resume "$sess" --json -m "$MODEL" "$prompt")
  else
    cmd=("$CODEX_BIN" exec --json -s read-only -C "$REPO_ROOT" -m "$MODEL" "$prompt")
  fi
  "${cmd[@]}" < /dev/null >"$outfile" 2>"$ERR_FILE"
  rc=$?

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local type
    type=$(echo "$line" | jq -r '.type // ""' 2>/dev/null)
    case "$type" in
      thread.started)
        [[ -z "$SESSION_ID" ]] && SESSION_ID=$(echo "$line" | jq -r '.thread_id // ""' 2>/dev/null)
        ;;
      item.completed)
        local itype
        itype=$(echo "$line" | jq -r '.item.type // ""' 2>/dev/null)
        if [[ "$itype" == "agent_message" ]]; then
          REPORT=$(echo "$line" | jq -r '.item.text // ""' 2>/dev/null)
        else
          render_event "$line"
        fi
        ;;
      turn.failed)
        turn_failed=1
        fail_msg=$(echo "$line" | jq -r '.error.message // "codex exec reported turn.failed"' 2>/dev/null)
        ;;
    esac
  done < "$outfile"
  rm -f "$outfile"

  if [[ -n "$turn_failed" ]]; then
    echo "$fail_msg" > "$ERR_FILE"
    return 1
  fi
  if [[ $rc -ne 0 ]]; then
    return 1
  fi
  return 0
}

emit() {  # emit <status> <session> <target> <lenses_csv> <report> <diagnostic>
  jq -nc \
    --arg status "$1" --arg session "$2" --arg target "$3" \
    --arg lenses "$4" --arg report "$5" --arg diag "$6" \
    '{status:$status, session_id:$session, target:$target,
      lenses:($lenses|split(",")|map(select(length>0))),
      report:$report, diagnostic:$diag}'
}

if ! run_codex "$PROMPT" "$SESSION"; then
  emit "BLOCKED" "$SESSION_ID" "$TARGET" "$LENSES" "" "codex exec invocation failed: $(cat "$ERR_FILE" 2>/dev/null)"
  exit 1
fi

if [[ -z "$REPORT" ]]; then
  emit "BLOCKED" "$SESSION_ID" "$TARGET" "$LENSES" "" "codex exec returned an empty review"
  exit 1
fi

emit "REVIEWED" "$SESSION_ID" "$TARGET" "$LENSES" "$REPORT" ""
exit 0
```

Make it executable:
```bash
chmod +x scripts/cx-delegate.sh
```

- [ ] **Step 5: Run the tests and make them pass**

```bash
bash tests/test-cx-delegate.sh
```
Expected: `PASS=<N> FAIL=0`, exit 0. If any check fails, read its `(expected [...], got [...])` line and fix the script — do not edit the test to match broken behavior.

- [ ] **Step 6: Commit**

```bash
git add tests/mock-codex scripts/cx-delegate.sh tests/test-cx-delegate.sh
git commit -m "$(cat <<'EOF'
feat: add cx-delegate.sh — Codex CLI delegate for the review script trio

Mirrors cr-delegate.sh's contract (arg surface, JSON envelope, exit
codes) against `codex exec` instead of `cursor-agent`, adding a
--repo-root/-C working-root rule that cr-delegate.sh doesn't need.
EOF
)"
```

---

### Task 2: `codex-reviewer-delegator` agent + `/codex-review` command + `models.json`

**Files:**
- Modify: `.claude-plugin/models.json`
- Create: `agents/codex-reviewer-delegator.md`
- Create: `commands/codex-review.md`
- Modify: `commands/cursor-review.md`

**Interfaces:**
- Consumes: `scripts/cx-delegate.sh` from Task 1 (path resolved via `${CLAUDE_PLUGIN_ROOT}/scripts/cx-delegate.sh` at agent-dispatch time).
- Produces: the `codex-reviewer-delegator` subagent (Haiku, tools `Bash, Read`) and the `/codex-review <spec|plan> <doc-path> [spec-path]` command, invocable exactly like `cursor-reviewer-delegator` / `/cursor-review`.

- [ ] **Step 1: Add the `codex_reviewer` role to `models.json`**

Read the current file, then edit it to:

```json
{
  "coder": {
    "id": "composer-2.5",
    "label": "Composer 2.5"
  },
  "reviewer": {
    "id": "cursor-grok-4.5-high-fast",
    "label": "Grok 4.5 (high effort, fast)"
  },
  "codex_reviewer": {
    "id": "gpt-5.6-sol",
    "label": "GPT-5.6 Sol"
  }
}
```

- [ ] **Step 2: Verify the model id resolves correctly**

```bash
jq -er '.codex_reviewer.id // empty' .claude-plugin/models.json
```
Expected output: `gpt-5.6-sol`.

- [ ] **Step 3: Create `agents/codex-reviewer-delegator.md`**

```markdown
---
name: codex-reviewer-delegator
description: Delegates an independent design-spec or implementation-plan review to <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label --> via the Codex CLI (`codex exec`) in a read-only sandbox, and relays the report. Use as the reviewer when an independent, unbiased review of a spec or plan is needed from a different vendor/model than the Cursor/Grok reviewer. Does not author, judge, or edit — it delegates and relays.
model: haiku
tools: Bash, Read
---

You are **codex-reviewer-delegator**, a delegator. You do NOT review, judge, or
write anything yourself. You hand one review job to <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label --> via a
bundled script that runs `codex exec` in a READ-ONLY sandbox, and you relay the result
back verbatim. The controller (Opus) decides what to do with the findings.

**You have no authority to author, edit, or decide the validity of findings.** You
have no `Write`/`Edit` tools; do not attempt to modify any file by other means
(`Bash` redirection, `sed`, `tee`). You do not filter, reorder, soften, or embellish
the reviewer's report — you pass it through exactly. If the script cannot produce a
review, that is a **BLOCKED** outcome you report — never something you paper over by
writing your own review.

## What you receive in your prompt
- The **target**: `spec` or `plan`.
- The **doc path**: the spec or plan file to review.
- Optionally, a **spec path** (for `plan` reviews — the spec the plan must satisfy).
- Optionally, **lenses** (for `spec` reviews — a comma-separated subset of
  `backend,frontend,ui`).
- Optionally, a **session id** to resume (for a follow-up review round).

## Your procedure
1. Run the delegate script (resolve its path via the plugin root):
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/cx-delegate.sh" \
     --target "<spec|plan>" \
     --doc-file "<doc path>" \
     [--spec-file "<spec path>" only for plan reviews when given] \
     [--lenses "<csv>" only for spec reviews when given] \
     [--session "<id>" only if you were given one to resume]
   ```
   You do NOT pass `--rubric-dir` or `--repo-root`; the script finds its bundled
   rubrics and resolves its own working root (via the doc's git repo, or its own
   plugin repo as fallback) next to itself.
2. The script prints ONE line of JSON to stdout:
   `{"status":..., "session_id":..., "target":..., "lenses":..., "report":..., "diagnostic":...}`
   Read it with `jq`. Everything on stderr is progress/diagnostics.
3. Report back (see format). ALWAYS include the `session_id` verbatim so the
   controller can dispatch a follow-up round that resumes the reviewer's context.

## Report format
- **Status:** REVIEWED | BLOCKED
- **Review:** the script's `report` field, reproduced VERBATIM and in full. Do not
  summarize, truncate, or edit it.
- **Reviewer session id:** the script's `session_id`, copied verbatim (may be empty
  together with BLOCKED if Codex never started a thread).
- **On BLOCKED:** include the script's `diagnostic` so the controller can decide.

## Rules
- You never author, edit, or judge. Relaying is your only job.
- If `codex` is unauthenticated or unavailable, the script returns BLOCKED — report
  that and stop. Fixing the environment is the controller's/user's job.
- Keep stdout parsing strict: only the script's final JSON line matters.
```

- [ ] **Step 4: Create `commands/codex-review.md`**

```markdown
---
description: Get an independent <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label --> review of a design spec or implementation plan, removing the bias of self-review. Usage: /codex-review <spec|plan> <doc-path> [spec-path]
argument-hint: <spec|plan> <doc-path> [spec-path]
---

You are the **controller**. You will obtain an INDEPENDENT review of the document
named in `$ARGUMENTS` by delegating to <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label --> through the
`codex-reviewer-delegator` subagent. You do NOT review it yourself — that is the
point: the model that authored the document must not be the one that grades it.

A second independent reviewer, <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->, is also
available via `/cursor-review`. Use whichever the user asked for; if they didn't say,
ask.

## Parse arguments
`$ARGUMENTS` is `<target> <doc-path> [spec-path]` where `target` is `spec` or `plan`.
- If `target` or `doc-path` is missing, ask the user for them and stop.
- `spec-path` is optional and used only for `plan` reviews (the spec the plan must satisfy).

## Preflight (do this first, stop on failure)

1. **Doc exists?** Confirm `doc-path` is a readable file. If not, tell the user and stop.
2. **codex healthy? Probe for real** — do not trust cached login state alone. Run an
   actual read-only headless probe:
   ```
   CODEX_REVIEWER_MODEL=$(jq -er '.codex_reviewer.id // empty' "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/models.json")
   if [[ -z "$CODEX_REVIEWER_MODEL" ]]; then
     echo "error: could not read .codex_reviewer.id from models.json"; exit 2
   fi
   codex exec --json -s read-only -m "$CODEX_REVIEWER_MODEL" "Reply with the single word READY." < /dev/null
   ```
   If the run exits non-zero, emits a `turn.failed` event, or the final `agent_message`
   text is not `READY` (auth error, timeout, or anything else), tell the user to run
   `codex login` (suggest they type `! codex login`) and STOP.

## Determine lenses (spec reviews only)

For `target spec`, decide which review lenses apply by reading the spec:
- **backend** — always include.
- **frontend** — include if the spec describes UI components, client state, routes,
  screens, or styling.
- **ui** — include if the spec describes user-facing flows, layouts, or UX.

If it is genuinely unclear whether the spec has a frontend/UI surface, ask the user
once. For `target plan`, do not pass lenses.

## Dispatch the reviewer

Dispatch the **`codex-reviewer-delegator`** subagent (NOT a general-purpose
subagent). Give it:
- the `target` (`spec` or `plan`),
- the `doc-path`,
- the `spec-path` if this is a plan review,
- the comma-separated `lenses` if this is a spec review.

It shells to the read-only reviewer and returns the report verbatim, plus a
`session_id` and a `status` (REVIEWED | BLOCKED).

If it returns **BLOCKED**, surface the diagnostic to the user and stop — do not
fabricate a review or substitute your own. Fixing the environment (login) is the
user's job.

## Act on the review (you, Opus)

Apply the **superpowers:receiving-code-review** skill to the returned report. Engage
each finding with technical rigor:
- Verify it against the document and the actual codebase before accepting it.
- Where the reviewer is right, plan or make the fix.
- Where the reviewer is wrong, push back with specific reasoning — do not perform
  agreement, and do not reflexively dismiss.

Present a triaged summary to the user (accepted / rejected-with-reason / needs-their-
decision). Offer a fix → re-review loop; on re-review, pass the prior `session_id` to
the subagent so the reviewer resumes its own context.

## Final step: Review Effectiveness Summary (ALWAYS do this)

Produce a short **Review Effectiveness Summary** (≈10–15 lines). Do BOTH: print it,
and append it as a dated entry to `docs/cursor-reviewer/effectiveness-log.md` in the
working repo (create the dir/file if missing; if not writable or the user objects,
just print it and say where it would have gone). Capture:

- **Reviewer:** Codex/GPT-5.6 Sol.
- **Run:** date · target · doc path · lenses used.
- **Findings:** count by severity (Critical/Important/Minor) · the reviewer's verdict.
- **Triage outcome:** how many findings you accepted vs pushed back on, and why.
- **Reviewer quality:** were findings specific and codebase-grounded, or vague? Any
  false positives (flagged a non-issue) or things it missed that you caught?
- **Environment friction:** auth/timeout, BLOCKED, missing `session_id`.
- **Recommendations:** concrete changes to the rubrics or dispatch prompt that would
  improve the next review.

Base every line on what actually happened this run — do not invent metrics.
```

- [ ] **Step 5: Add a discoverability pointer to `commands/cursor-review.md`**

Find this line near the top of `commands/cursor-review.md`:

```
`cursor-reviewer-delegator` subagent. You do NOT review it yourself — that is the
point: the model that authored the document must not be the one that grades it.
```

Add immediately after it (before the `## Parse arguments` heading):

```

A second independent reviewer, <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->, is also
available via `/codex-review`. Use whichever the user asked for; if they didn't say,
ask.
```

- [ ] **Step 6: Sanity-check the new files parse and the preflight probe works**

```bash
python3 -c "import re,sys; [sys.exit(1) for f in ['agents/codex-reviewer-delegator.md','commands/codex-review.md'] if not re.search(r'^---\n.*?\n---\n', open(f).read(), re.S)]" && echo "frontmatter OK"

CODEX_REVIEWER_MODEL=$(jq -er '.codex_reviewer.id // empty' .claude-plugin/models.json)
codex exec --json -s read-only -m "$CODEX_REVIEWER_MODEL" "Reply with the single word READY." < /dev/null
```
Expected: `frontmatter OK`, then JSONL ending in a `turn.completed` event with the prior `agent_message` item's text being `READY`.

- [ ] **Step 7: Commit**

```bash
git add .claude-plugin/models.json agents/codex-reviewer-delegator.md commands/codex-review.md commands/cursor-review.md
git commit -m "$(cat <<'EOF'
feat: add codex-reviewer-delegator agent and /codex-review command

Adds the codex_reviewer role to models.json and wires up a Haiku
relay-only subagent + controller command mirroring
cursor-reviewer-delegator / /cursor-review, targeting Codex/GPT-5.6
Sol instead of cursor-agent/Grok. Cross-links both commands so either
entry point surfaces the other reviewer.
EOF
)"
```

---

### Task 3: Three-way `sync-models.sh` + extended `test-sync-models.sh`

**Files:**
- Modify: `scripts/sync-models.sh`
- Modify: `tests/test-sync-models.sh`

**Interfaces:**
- Consumes: `.claude-plugin/models.json`'s `codex_reviewer.id`/`.label` (from Task 2).
- Produces: `regen_markers(file, which)` now dispatches `coder|reviewer|codex_reviewer` (previously binary `coder`/else); `MARKER_TARGETS` includes 5 new `:codex_reviewer` entries; `PLUGIN_DESC`/`MARKETPLACE_DESC` name all three models.

- [ ] **Step 1: Extend `tests/test-sync-models.sh`'s fixture repo (failing first)**

In `tests/test-sync-models.sh`, replace the `make_fixture_repo` function's models.json heredoc:

Old:
```bash
  cat > "$d/.claude-plugin/models.json" <<'EOF'
{"coder": {"id": "fixture-coder", "label": "Fixture Coder Label"}, "reviewer": {"id": "fixture-reviewer", "label": "Fixture Reviewer Label"}}
EOF
```

New:
```bash
  cat > "$d/.claude-plugin/models.json" <<'EOF'
{"coder": {"id": "fixture-coder", "label": "Fixture Coder Label"}, "reviewer": {"id": "fixture-reviewer", "label": "Fixture Reviewer Label"}, "codex_reviewer": {"id": "fixture-codex", "label": "Fixture Codex Label"}}
EOF
```

Add two new fixture files to `make_fixture_repo`, right after the `cursor-reviewer-delegator.md` heredoc:

```bash
  cat > "$d/agents/codex-reviewer-delegator.md" <<'EOF'
---
name: codex-reviewer-delegator
description: placeholder
model: haiku
tools: Bash, Read
---
You hand one review job to <!-- model:codex_reviewer:label -->old label<!-- /model:codex_reviewer:label --> via a script.
EOF
```

And right after the `cursor-review.md` heredoc:

```bash
  cat > "$d/commands/codex-review.md" <<'EOF'
---
description: placeholder
argument-hint: <path>
---
named in `$ARGUMENTS` by delegating to <!-- model:codex_reviewer:label -->old label<!-- /model:codex_reviewer:label --> through the subagent.
EOF
```

Add a fixture script header line right after `cr-delegate.sh`'s heredoc:

```bash
  cat > "$d/scripts/cx-delegate.sh" <<'EOF'
#!/usr/bin/env bash
# cx-delegate.sh — delegate ONE review to <!-- model:codex_reviewer:label -->old label<!-- /model:codex_reviewer:label -->.
echo unrelated body
EOF
```

Extend the README fixture (replace the whole heredoc) to include a `codex_reviewer` row:

```bash
  cat > "$d/README.md" <<'EOF'
# fixture readme

- **Implementation** is delegated to Cursor's **<!-- model:coder:label -->old label<!-- /model:coder:label -->**.
- **Independent review** is delegated to **<!-- model:reviewer:label -->old label<!-- /model:reviewer:label -->** or **<!-- model:codex_reviewer:label -->old label<!-- /model:codex_reviewer:label -->**.

| agent | Delegates to |
|---|---|
| coder | <!-- model:coder:label -->old label<!-- /model:coder:label --> |
| reviewer | <!-- model:reviewer:label -->old label<!-- /model:reviewer:label --> (read-only) |
| codex_reviewer | <!-- model:codex_reviewer:label -->old label<!-- /model:codex_reviewer:label --> (read-only) |

Usage: (delegates to <!-- model:reviewer:label -->old label<!-- /model:reviewer:label --> or <!-- model:codex_reviewer:label -->old label<!-- /model:codex_reviewer:label -->)
EOF
```

Extend the `e2e-smoke.md` fixture heredoc to add a second heading:

```bash
  cat > "$d/tests/e2e-smoke.md" <<'EOF'
# Reviewer delegation (cr-delegate.sh — <!-- model:reviewer:label -->old label<!-- /model:reviewer:label -->)
unrelated body

# Codex reviewer delegation (cx-delegate.sh — <!-- model:codex_reviewer:label -->old label<!-- /model:codex_reviewer:label -->)
unrelated body
EOF
```

- [ ] **Step 2: Add new assertions to the happy-path block**

Immediately after the existing `check "e2e heading marker updated" ...` line (before `rm -rf "$REPO"` at the end of the happy-path block), add:

```bash
check "plugin.json description mentions codex_reviewer label" "1" \
  "$(jq -r '.description' "$REPO/.claude-plugin/plugin.json" | grep -c 'Fixture Codex Label')"
check "marketplace.json description mentions codex_reviewer label" "1" \
  "$(jq -r '.plugins[0].description' "$REPO/.claude-plugin/marketplace.json" | grep -c 'Fixture Codex Label')"
check "codex_reviewer agent frontmatter description updated" "1" \
  "$(grep '^description:' "$REPO/agents/codex-reviewer-delegator.md" | grep -c 'Fixture Codex Label')"
check "codex_reviewer command frontmatter description updated" "1" \
  "$(grep '^description:' "$REPO/commands/codex-review.md" | grep -c 'Fixture Codex Label')"
check "README codex_reviewer table cell updated, read-only suffix survives" "1" \
  "$(grep -c 'Fixture Codex Label.*(read-only)' "$REPO/README.md")"
check "README reviewer table cell STILL updated (no cross-write)" "1" \
  "$(grep -c 'Fixture Reviewer Label.*(read-only)' "$REPO/README.md")"
check "README no old-label text remains" "0" "$(grep -c 'old label' "$REPO/README.md")"
check "codex_reviewer script header marker updated, stays on comment line" "1" \
  "$(grep -c '^# cx-delegate.sh.*Fixture Codex Label' "$REPO/scripts/cx-delegate.sh")"
check "codex_reviewer script body untouched" "1" "$(grep -c 'unrelated body' "$REPO/scripts/cx-delegate.sh")"
check "e2e codex_reviewer heading marker updated" "1" \
  "$(grep -c 'Fixture Codex Label' "$REPO/tests/e2e-smoke.md")"
check "e2e reviewer heading STILL updated (no cross-write)" "1" \
  "$(grep -c 'Fixture Reviewer Label' "$REPO/tests/e2e-smoke.md")"
```

The last two checks in each pair (README, e2e) are the cross-write regression guard the independent review specifically flagged: if `regen_markers` stayed a binary `if/else`, the `codex_reviewer` marker span would get `Fixture Reviewer Label` instead of `Fixture Codex Label`, and these `grep -c 'Fixture Codex Label...'` checks would fail while the mislabeled span would silently contain the wrong text.

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
bash tests/test-sync-models.sh
```
Expected: multiple `FAIL` lines for the new `codex_reviewer`-related checks (the script doesn't populate that role at all yet), and likely a hard error/`FAIL` for the missing-marker case too since `regen_markers` will error out on `agents/codex-reviewer-delegator.md`'s marker not being recognized — that's fine, confirms the starting state.

- [ ] **Step 4: Implement the `sync-models.sh` changes**

In `scripts/sync-models.sh`, after the existing:
```bash
REVIEWER_ID="$(jq -r '.reviewer.id // empty' "$MODELS_JSON")"
REVIEWER_LABEL="$(jq -r '.reviewer.label // empty' "$MODELS_JSON")"
```
add:
```bash
CODEX_REVIEWER_ID="$(jq -r '.codex_reviewer.id // empty' "$MODELS_JSON")"
CODEX_REVIEWER_LABEL="$(jq -r '.codex_reviewer.label // empty' "$MODELS_JSON")"
```

Change the validation loop line:
```bash
for name_val in "coder.id:$CODER_ID" "coder.label:$CODER_LABEL" "reviewer.id:$REVIEWER_ID" "reviewer.label:$REVIEWER_LABEL"; do
```
to:
```bash
for name_val in "coder.id:$CODER_ID" "coder.label:$CODER_LABEL" "reviewer.id:$REVIEWER_ID" "reviewer.label:$REVIEWER_LABEL" "codex_reviewer.id:$CODEX_REVIEWER_ID" "codex_reviewer.label:$CODEX_REVIEWER_LABEL"; do
```

After the existing:
```bash
if [[ ! "$REVIEWER_ID" =~ $ID_RE ]]; then
  echo "error: .reviewer.id '$REVIEWER_ID' violates the allowed charset $ID_RE" >&2; exit 2
fi
```
add:
```bash
if [[ ! "$CODEX_REVIEWER_ID" =~ $ID_RE ]]; then
  echo "error: .codex_reviewer.id '$CODEX_REVIEWER_ID' violates the allowed charset $ID_RE" >&2; exit 2
fi
```

After the existing:
```bash
check_label_charset "coder.label" "$CODER_LABEL"
check_label_charset "reviewer.label" "$REVIEWER_LABEL"
```
add:
```bash
check_label_charset "codex_reviewer.label" "$CODEX_REVIEWER_LABEL"
```

Replace the two description-builder lines:
```bash
PLUGIN_DESC="Delegate implementation to Cursor's ${CODER_LABEL} and independent design-spec/plan review to ${REVIEWER_LABEL} via cursor-agent, while Claude/Opus plans, decides, and reviews."
MARKETPLACE_DESC="Two Cursor-backed delegation subagents: ${CODER_LABEL} for implementation and ${REVIEWER_LABEL} for independent design/plan review."
```
with:
```bash
PLUGIN_DESC="Delegate implementation to Cursor's ${CODER_LABEL}, and independent design-spec/plan review to ${REVIEWER_LABEL} via cursor-agent or ${CODEX_REVIEWER_LABEL} via the Codex CLI, while Claude/Opus plans, decides, and reviews."
MARKETPLACE_DESC="Delegation subagents: ${CODER_LABEL} for implementation, and independent design/plan review via ${REVIEWER_LABEL} (cursor-agent) or ${CODEX_REVIEWER_LABEL} (Codex CLI)."
```

After the existing:
```bash
REVIEWER_AGENT_DESC="Delegates an independent design-spec or implementation-plan review to ${REVIEWER_LABEL} via cursor-agent in read-only mode, and relays the report. Use as the reviewer when an independent, unbiased review of a spec or plan is needed. Does not author, judge, or edit — it delegates and relays."
CODER_CMD_DESC="Implement a written plan by delegating each task to Cursor's ${CODER_LABEL} (via the cursor-coder-delegator subagent), while Opus reviews. Usage: /cursor-implement-plans <plan-path>"
REVIEWER_CMD_DESC="Get an independent ${REVIEWER_LABEL} review of a design spec or implementation plan, removing the bias of self-review. Usage: /cursor-review <spec|plan> <doc-path> [spec-path]"
```
add:
```bash
CODEX_REVIEWER_AGENT_DESC="Delegates an independent design-spec or implementation-plan review to ${CODEX_REVIEWER_LABEL} via the Codex CLI (\`codex exec\`) in a read-only sandbox, and relays the report. Use as the reviewer when an independent, unbiased review of a spec or plan is needed from a different vendor/model than the Cursor/Grok reviewer. Does not author, judge, or edit — it delegates and relays."
CODEX_REVIEWER_CMD_DESC="Get an independent ${CODEX_REVIEWER_LABEL} review of a design spec or implementation plan, removing the bias of self-review. Usage: /codex-review <spec|plan> <doc-path> [spec-path]"
```

After the existing:
```bash
regen_frontmatter_description "$REPO_DIR/agents/cursor-coder-delegator.md" "$CODER_AGENT_DESC"
regen_frontmatter_description "$REPO_DIR/agents/cursor-reviewer-delegator.md" "$REVIEWER_AGENT_DESC"
regen_frontmatter_description "$REPO_DIR/commands/cursor-implement-plans.md" "$CODER_CMD_DESC"
regen_frontmatter_description "$REPO_DIR/commands/cursor-review.md" "$REVIEWER_CMD_DESC"
```
add:
```bash
regen_frontmatter_description "$REPO_DIR/agents/codex-reviewer-delegator.md" "$CODEX_REVIEWER_AGENT_DESC"
regen_frontmatter_description "$REPO_DIR/commands/codex-review.md" "$CODEX_REVIEWER_CMD_DESC"
```

Replace the whole `regen_markers` function:
```bash
# regen_markers <file> <which: coder|reviewer>
regen_markers() {
  local file="$1" which="$2" value tag
  [[ ! -f "$file" ]] && { echo "error: expected file missing: $file" >&2; exit 2; }
  if [[ "$which" == "coder" ]]; then value="$CODER_LABEL"; else value="$REVIEWER_LABEL"; fi
  tag="model:$which:label"
```
with:
```bash
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
```
(the rest of the function body — the `grep -q`, `mktemp_for`, `perl` substitution, `atomic_write` call — is unchanged).

Replace the `MARKER_TARGETS` array:
```bash
MARKER_TARGETS=(
  "$REPO_DIR/README.md:coder"
  "$REPO_DIR/README.md:reviewer"
  "$REPO_DIR/agents/cursor-coder-delegator.md:coder"
  "$REPO_DIR/agents/cursor-reviewer-delegator.md:reviewer"
  "$REPO_DIR/commands/cursor-implement-plans.md:coder"
  "$REPO_DIR/commands/cursor-review.md:reviewer"
  "$REPO_DIR/scripts/cc-delegate.sh:coder"
  "$REPO_DIR/scripts/cr-delegate.sh:reviewer"
  "$REPO_DIR/tests/e2e-smoke.md:reviewer"
)
```
with:
```bash
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
```

- [ ] **Step 5: Run the tests and make them pass**

```bash
bash tests/test-sync-models.sh
```
Expected: `PASS=<N> FAIL=0`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/sync-models.sh tests/test-sync-models.sh
git commit -m "$(cat <<'EOF'
fix: make sync-models.sh's regen_markers a three-way dispatch

The prior coder/else-reviewer branch would have silently written
Grok's label into any codex_reviewer marker span once a third role
existed. Adds explicit codex_reviewer handling plus a regression test
that fails if any role's label leaks into another role's marker.
EOF
)"
```

---

### Task 4: Extend `test-drift-coverage.sh` for the Codex reviewer

**Files:**
- Modify: `tests/test-drift-coverage.sh`

**Interfaces:**
- Consumes: `scripts/cx-delegate.sh` (Task 1), `commands/codex-review.md` (Task 2) — both must exist already.

- [ ] **Step 1: Add the failing checks**

After the existing:
```bash
no_literal_model "e2e-smoke.md probe has no hardcoded --model" "$REPO/tests/e2e-smoke.md"
```
add:
```bash
no_literal_model_codex() {  # no_literal_model_codex <description> <file>
  check "$1" "0" "$(grep -Ec -- '--?m(odel)?[[:space:]]+"?gpt-5\.6-sol"?' "$2")"
}
no_literal_model_codex "cx-delegate.sh has no hardcoded -m/--model" "$REPO/scripts/cx-delegate.sh"
no_literal_model_codex "codex-review.md healthcheck has no hardcoded -m/--model" "$REPO/commands/codex-review.md"
no_literal_model_codex "e2e-smoke.md codex probe has no hardcoded -m/--model" "$REPO/tests/e2e-smoke.md"
```

After the existing:
```bash
check "e2e-smoke.md reads REVIEWER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.reviewer\.id' "$REPO/tests/e2e-smoke.md")"
```
add:
```bash
check "cx-delegate.sh reads MODEL from jq" "1" "$(grep -c 'jq -r .*\.codex_reviewer\.id' "$REPO/scripts/cx-delegate.sh")"
check "codex-review.md reads CODEX_REVIEWER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.codex_reviewer\.id' "$REPO/commands/codex-review.md")"
check "e2e-smoke.md reads CODEX_REVIEWER_MODEL from jq" "1" "$(grep -c 'jq -er .*\.codex_reviewer\.id' "$REPO/tests/e2e-smoke.md")"
```

Note: `e2e-smoke.md`'s Codex section is written in Task 5 — this task's last two `e2e-smoke.md` checks will fail until Task 5 lands. That's expected and acceptable since these are additive checks in a shared file across two tasks in the same plan; do not skip writing them here.

- [ ] **Step 2: Run to confirm the currently-addressable checks fail correctly**

```bash
bash tests/test-drift-coverage.sh
```
Expected: `FAIL` on every new check (files don't exist with the right content yet, or don't exist at all for `codex-review.md` if Task 2 wasn't run first — it was, so `codex-review.md`'s checks should already look sane; the `e2e-smoke.md` ones fail until Task 5).

- [ ] **Step 3: Commit**

```bash
git add tests/test-drift-coverage.sh
git commit -m "test: guard the Codex reviewer's dynamic --model probes against hardcoded regressions"
```

(This will show 3 failing checks — the `e2e-smoke.md` ones — until Task 5 completes; that's expected mid-plan and resolved in Task 6's full-suite run.)

---

### Task 5: README + e2e-smoke.md updates

**Files:**
- Modify: `README.md`
- Modify: `tests/e2e-smoke.md`

- [ ] **Step 1: Update the README's intro bullets**

Find:
```markdown
- **Implementation** is delegated to Cursor's **<!-- model:coder:label -->Composer 2.5<!-- /model:coder:label -->**.
- **Independent review** of a design spec or plan is delegated to **<!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->** —
  so the model that authored a doc is never the model that grades it.
```
Replace with:
```markdown
- **Implementation** is delegated to Cursor's **<!-- model:coder:label -->Composer 2.5<!-- /model:coder:label -->**.
- **Independent review** of a design spec or plan is delegated to **<!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->**
  via `cursor-agent`, or to **<!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->** via the Codex CLI (`codex exec`) —
  two independently-sourced reviewers, so the model that authored a doc is never the
  model that grades it, and you choose which one reviews each time.
```

- [ ] **Step 2: Add a table row**

Find:
```markdown
| `cursor-reviewer-delegator` | `/cursor-review <spec\|plan> <doc-path> [spec-path]` | <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label --> (read-only) | Runs the review script and relays the report verbatim. Cannot author or judge. |
```
Add immediately after it:
```markdown
| `codex-reviewer-delegator` | `/codex-review <spec\|plan> <doc-path> [spec-path]` | <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label --> (read-only) | Runs the Codex delegate script and relays the report verbatim. Cannot author or judge. |
```

- [ ] **Step 3: Add a Requirements bullet**

Find:
```markdown
## Requirements
- `cursor-agent` installed and logged in (`cursor-agent status` / `cursor-agent login`).
```
Replace with:
```markdown
## Requirements
- `cursor-agent` installed and logged in (`cursor-agent status` / `cursor-agent login`).
- `codex` CLI installed and logged in (`codex login status` / `codex login`).
```

- [ ] **Step 4: Update the Usage section's review examples and "where it fits" paragraph**

Find:
```markdown
**Review a spec or plan** (delegates to <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->):
- Spec: `/cursor-review spec docs/superpowers/specs/2026-01-01-foo-design.md`
- Plan vs spec: `/cursor-review plan docs/superpowers/plans/2026-01-01-foo.md docs/superpowers/specs/2026-01-01-foo-design.md`

Where it fits the superpowers flow: run `/cursor-review spec <spec-path>` at the
brainstorming Spec self-review gate, and `/cursor-review plan <plan-path> <spec-path>`
at the writing-plans Self-Review. For spec reviews the controller auto-selects review
**lenses** (backend always; frontend/ui when the spec has a UI surface).

Review is code- and document-based, not visual: `cursor-agent` cannot render or
screenshot a UI.
```
Replace with:
```markdown
**Review a spec or plan** (delegates to <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->
or <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->):
- Cursor/Grok, spec: `/cursor-review spec docs/superpowers/specs/2026-01-01-foo-design.md`
- Codex/GPT-5.6 Sol, spec: `/codex-review spec docs/superpowers/specs/2026-01-01-foo-design.md`
- Cursor/Grok, plan vs spec: `/cursor-review plan docs/superpowers/plans/2026-01-01-foo.md docs/superpowers/specs/2026-01-01-foo-design.md`
- Codex/GPT-5.6 Sol, plan vs spec: `/codex-review plan docs/superpowers/plans/2026-01-01-foo.md docs/superpowers/specs/2026-01-01-foo-design.md`

Where it fits the superpowers flow: at the brainstorming Spec self-review gate and the
writing-plans Self-Review gate, **ask the user which reviewer to use** — Cursor/Grok or
Codex/GPT-5.6 Sol — then run `/cursor-review spec|plan ...` or `/codex-review spec|plan
...` accordingly. This is a documentation convention (the superpowers skills themselves
aren't edited); an explicit `/cursor-review` or `/codex-review` invocation is already
the user's choice and needs no extra prompt. For spec reviews both commands
auto-select review **lenses** (backend always; frontend/ui when the spec has a UI
surface).

Review is code- and document-based, not visual: neither `cursor-agent` nor `codex` can
render or screenshot a UI.
```

- [ ] **Step 5: Add the new test to the Tests section**

Find:
```markdown
bash tests/test-cr-delegate.sh       # reviewer delegate unit tests (mock cursor-agent)
```
Add immediately after it:
```markdown
bash tests/test-cx-delegate.sh       # Codex reviewer delegate unit tests (mock codex)
```

- [ ] **Step 6: Append the Codex e2e-smoke section**

Append to the end of `tests/e2e-smoke.md`:

```markdown

---

# Codex reviewer delegation (cx-delegate.sh — <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->)

Prereqs: `codex login status` shows logged in; the probe below returns `READY`:
```bash
CODEX_REVIEWER_MODEL=$(jq -er '.codex_reviewer.id // empty' "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/models.json")
if [[ -z "$CODEX_REVIEWER_MODEL" ]]; then
  echo "error: could not read .codex_reviewer.id from models.json"; exit 2
fi
codex exec --json -s read-only -m "$CODEX_REVIEWER_MODEL" "Reply with the single word READY." < /dev/null
```

## Setup
```bash
tmp=$(mktemp -d); cd "$tmp"
git init -q && git commit -q --allow-empty -m "init"
cat > spec.md <<'EOF'
# Widget Cache Design
Goal: add an in-memory cache for widget lookups.
Architecture: a singleton map keyed by widget id, no eviction.
EOF
```

## R1. Spec review (happy path)
```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target spec --doc-file "$tmp/spec.md" --lenses backend
```
Expect: stdout is ONE JSON line with `"status":"REVIEWED"`, a non-empty `"report"`,
a real `"session_id"`, and `"lenses":["backend"]`. The report should follow the
output format (Summary / Strengths / Issues / Verdict) and likely flag the "no
eviction" unbounded-growth risk. No files in `$tmp` were modified.

## R2. Plan review against a spec
```bash
cat > plan.md <<'EOF'
# Widget Cache Implementation Plan
Task 1: add cache.py with get(id) and set(id, val).
EOF
bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target plan --doc-file "$tmp/plan.md" --spec-file "$tmp/spec.md"
```
Expect: `"status":"REVIEWED"`, `"target":"plan"`; the report should note missing
eviction coverage / thin task decomposition relative to the spec.

## R3. BLOCKED path (logged out)
Temporarily log out (or unset auth) and re-run command R1. Expect `"status":"BLOCKED"`,
exit code 1, and a `diagnostic` explaining the failure. No fabricated review.

## R4. Resume across rounds
```bash
SESSION=$(bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target spec --doc-file "$tmp/spec.md" --lenses backend | jq -r .session_id)
bash "$CLAUDE_PLUGIN_ROOT/scripts/cx-delegate.sh" \
  --target spec --doc-file "$tmp/spec.md" --lenses backend --session "$SESSION"
```
Expect: second call also returns `"status":"REVIEWED"` and the same `"session_id"`,
demonstrating `codex exec resume` picks the thread back up.

## Cleanup
```bash
rm -rf "$tmp"
```
```

- [ ] **Step 7: Run the drift-coverage and sync-models tests again — they should now be fully green**

```bash
bash tests/test-drift-coverage.sh
bash tests/test-sync-models.sh
```
Expected: both `PASS=<N> FAIL=0`.

- [ ] **Step 8: Commit**

```bash
git add README.md tests/e2e-smoke.md
git commit -m "$(cat <<'EOF'
docs: document the Codex reviewer in README and e2e-smoke.md

Adds the third table row, requirement, usage examples, and the
ask-which-reviewer gate convention to README.md, plus a manual e2e
section for cx-delegate.sh mirroring the existing Cursor one
(including a resume-across-rounds check).
EOF
)"
```

---

### Task 6: Final wiring — `plugin.json` keywords, real `sync-models.sh` run, full suite

**Files:**
- Modify: `.claude-plugin/plugin.json`

- [ ] **Step 1: Add `codex` to `plugin.json`'s keywords**

Read `.claude-plugin/plugin.json`, then edit its `keywords` array from:
```json
  "keywords": [
    "cursor",
    "composer",
    "delegation",
    "review",
    "subagent",
    "superpowers"
  ]
```
to:
```json
  "keywords": [
    "cursor",
    "composer",
    "codex",
    "delegation",
    "review",
    "subagent",
    "superpowers"
  ]
```

- [ ] **Step 2: Run `sync-models.sh` for real against the live repo**

```bash
bash scripts/sync-models.sh
git diff --stat
```
Expected: `plugin.json`'s `.description`, `marketplace.json`'s `.plugins[0].description`, and every marker span / frontmatter `description:` line touched in Tasks 1, 2, and 5 are normalized to exactly match `models.json`'s current labels — this is the single source of truth check that everything authored by hand in earlier tasks was written correctly. `git diff --stat` should show only description/marker-span text changes (if any) — no structural changes.

- [ ] **Step 3: `--check` must now report zero drift**

```bash
bash scripts/sync-models.sh --check
echo "exit=$?"
```
Expected: `exit=0`, no `stale (would change on sync):` output.

- [ ] **Step 4: Run the complete test suite**

```bash
bash tests/test-cc-delegate.sh
bash tests/test-cr-delegate.sh
bash tests/test-cx-delegate.sh
bash tests/test-gen-changelog.sh
bash tests/test-update-changelog.sh
bash tests/test-sync-models.sh
bash tests/test-drift-coverage.sh
```
Expected: every script prints `PASS=<N> FAIL=0` and exits 0.

- [ ] **Step 5: Live smoke check of the new reviewer against this repo**

```bash
bash scripts/cx-delegate.sh --target spec --doc-file docs/superpowers/specs/2026-07-29-codex-reviewer-design.md --lenses backend | jq '{status, target, lenses, session_id, report_len: (.report | length)}'
```
Expected: `status` is `"REVIEWED"`, `target` is `"spec"`, `lenses` is `["backend"]`, `session_id` is a non-empty UUID, `report_len` is greater than 0.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/plugin.json .claude-plugin/marketplace.json README.md agents/codex-reviewer-delegator.md commands/codex-review.md scripts/cx-delegate.sh tests/e2e-smoke.md
git commit -m "$(cat <<'EOF'
chore: normalize Codex reviewer descriptions/markers via sync-models.sh

Adds "codex" to plugin.json keywords and runs sync-models.sh for real
so every description and marker span exactly matches models.json.
Full test suite is green; sync-models.sh --check reports zero drift.
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- Independent spec/plan review via Codex/GPT-5.6 Sol → Task 1 (`cx-delegate.sh`), Task 2 (agent/command).
- Same lens model, rubrics reused verbatim → Task 1's rubric-dir default and prompt assembly; no `rubrics/` files touched anywhere in this plan.
- Read-only sandbox grounding + working-root (`-C`/`--repo-root`) → Task 1 Step 4 (script), tested by the git-root/explicit-root/non-git-fallback cases in `test-cx-delegate.sh`.
- Ask-which-reviewer convention + cross-command discoverability → Task 2 Step 5 (`cursor-review.md` pointer), Task 5 Steps 1 and 4 (README).
- `models.json` third role → Task 2 Step 1.
- `sync-models.sh` three-way `regen_markers` fix → Task 3.
- `plugin.json`/`marketplace.json` description regeneration + manual `keywords` edit → Task 6.
- Drift-coverage guards → Task 4.
- README + e2e-smoke documentation → Task 5.
- Exit codes / error-handling table from the spec → encoded directly in Task 1's script and asserted in its tests (CLI failure, `turn.failed`, empty report, all → BLOCKED/exit 1; validation → exit 2).
- Security/observability posture (no `--dangerously-bypass-*`, no timeout change) → never introduced anywhere in this plan; nothing to add, by omission.
- Out-of-scope items (combined dual-review command, code review, plugin rename, changing the existing reviewer) → none of the six tasks touch `cr-delegate.sh`, `cursor-reviewer-delegator.md`'s body, or `models.json`'s `reviewer` key; no combined command is created.

No gaps found.

**Placeholder scan:** no TBD/TODO markers; every code block is complete, runnable content, not a description of what to write.

**Type/interface consistency:** `cx-delegate.sh`'s flag surface (`--target`, `--doc-file`, `--spec-file`, `--lenses`, `--rubric-dir`, `--session`, `--repo-root`) is identical across Task 1 (implementation), Task 2 (agent's invocation instructions), and Task 5 (e2e-smoke examples). The JSON envelope field names (`status`, `session_id`, `target`, `lenses`, `report`, `diagnostic`) match between Task 1's `emit()` and Task 2's agent report-format section. `models.json`'s `codex_reviewer.id`/`.label` keys are read identically in Task 1 (`cx-delegate.sh`), Task 2 (`codex-review.md` preflight), Task 3 (`sync-models.sh`), and Task 4 (drift guards).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-29-codex-reviewer.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
