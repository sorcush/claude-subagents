# Reviewer and Coder Pools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the plugin's three hard-coded model roles with two configurable pools and a pluggable harness layer, so changing a model is a one-line JSON edit and adding a tool is one new file.

**Architecture:** Two JSON pool files describe the available reviewers and coders. A shared library reads and validates them. A harness file per tool (`cursor`, `codex`, `claude`) hides how that tool is invoked behind three shell functions. Two generic delegate scripts replace three tool-specific ones. The orchestrator builds its menu at run time from the pool, so no command or agent file ever names a model. The coder works in a git worktree whose dependency folders are copy-on-write clones, so it cannot write into the main checkout.

**Tech Stack:** Bash 5, `jq`, `git`, and the `cursor-agent`, `codex` and `claude` CLIs. Tests are plain bash scripts using fake CLI programs placed on `PATH`, following the existing `tests/mock-*` pattern.

**Spec:** `docs/superpowers/specs/2026-08-22-reviewer-coder-pools-design.md`

## Global Constraints

These apply to every task. They are copied from the spec and are not repeated in each task's steps.

- **Output discipline.** Every script prints exactly one line of JSON to stdout. All progress, diagnostics and errors go to stderr. Nothing else may reach stdout.
- **No model names in prompts.** No file in `commands/` or `agents/` may contain any model id or model label. Task 8 adds a test that enforces this.
- **`harness_run` is called as a plain statement**, never inside `$(...)` or a pipeline. It returns results by setting shell variables, which a subshell would discard.
- **Harness functions write nothing to stdout.** Stdout belongs to the calling script's JSON line.
- **Every external call is bounded by a timeout**, and the timeout kills the whole process group, not just the direct child.
- **Pool field rules.** `key` matches `^[a-z0-9][a-z0-9-]*$`; `harness` matches `^[a-z][a-z0-9-]*$`; `model` matches `^[A-Za-z0-9._+-]+$`; `label` is non-empty printable ASCII with no newline; `default` is a boolean on at most one entry.
- **Timeout defaults.** 120 seconds for a probe (`CSC_PROBE_TIMEOUT`), 1800 seconds for a run (`CSC_RUN_TIMEOUT`).
- **Never use a `-fast` model variant**, for any provider. They cost more.
- **Confirmed model ids.** `gpt-5.6-sol`, `gpt-5.6-luna` (codex); `cursor-grok-4.6-high`, `gpt-5.6-sol-high`, `composer-2.5` (cursor); `claude-opus-5` (claude).
- **Tests follow the existing idiom**: a `check <desc> <expected> <actual>` helper, `PASS`/`FAIL` counters, a `PASS=n FAIL=n` summary line, and `[[ "$FAIL" -eq 0 ]]` as the last line.

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `.claude-plugin/reviewers.json` | The reviewer pool. |
| `.claude-plugin/coders.json` | The coder pool. |
| `scripts/lib/pool.sh` | Sourced. Validate a pool file; list entries; look up one entry. |
| `scripts/lib/timeout.sh` | Sourced. Bounded execution with process-group cleanup. |
| `scripts/pool.sh` | CLI front end over `lib/pool.sh`, so the orchestrator can build a menu. |
| `scripts/harness/cursor.sh` | How to drive `cursor-agent`. |
| `scripts/harness/codex.sh` | How to drive `codex`. |
| `scripts/harness/claude.sh` | How to drive `claude`. |
| `scripts/probe.sh` | Health check one pool entry through its harness. |
| `scripts/review-delegate.sh` | One review, any reviewer, read-only. |
| `scripts/code-delegate.sh` | One coding task, any coder, with verify and retry, inside a worktree. |
| `scripts/worktree.sh` | Create and remove the coder's worktree. |
| `agents/reviewer-delegator.md` | Subagent that runs `review-delegate.sh` and relays. |
| `agents/coder-delegator.md` | Subagent that runs `code-delegate.sh`, commits in the worktree, and reports. |
| `commands/review.md` | `/review` orchestrator. |
| `commands/implement-plans.md` | `/implement-plans` orchestrator. |
| `tests/mock-claude` | Fake `claude` CLI. |
| `tests/test-pool.sh` | Pool parsing and validation. |
| `tests/test-timeout.sh` | Timeout helper, including child-process cleanup. |
| `tests/test-probe.sh` | Probe across all three harnesses. |
| `tests/test-review-delegate.sh` | Reviewer path across all three harnesses. |
| `tests/test-code-delegate.sh` | Coder path across all three harnesses, and cwd handling. |
| `tests/test-worktree.sh` | Worktree lifecycle against a real temporary repository. |
| `tests/test-worktree-isolation.sh` | Proves the coder cannot write into the main checkout. |
| `tests/test-no-model-names.sh` | Guards that no model name reaches the orchestrator. |

**Deleted**

`scripts/sync-models.sh`, `scripts/cc-delegate.sh`, `scripts/cr-delegate.sh`, `scripts/cx-delegate.sh`, `.claude-plugin/models.json`, `agents/cursor-coder-delegator.md`, `agents/cursor-reviewer-delegator.md`, `agents/codex-reviewer-delegator.md`, `commands/cursor-implement-plans.md`, `commands/cursor-review.md`, `commands/codex-review.md`, `tests/test-sync-models.sh`, `tests/test-drift-coverage.sh`, `tests/test-cc-delegate.sh`, `tests/test-cr-delegate.sh`, `tests/test-cx-delegate.sh`.

**Modified**

`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `Makefile`, `README.md`, `CHANGELOG.md`, `tests/e2e-smoke.md`.

---

### Task 1: Pool files, pool library, and `pool.sh`

**Files:**
- Create: `.claude-plugin/reviewers.json`, `.claude-plugin/coders.json`, `scripts/lib/pool.sh`, `scripts/pool.sh`
- Test: `tests/test-pool.sh`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `pool_load <file> <role>` — validates and sets `POOL_JSON` (the file's contents). Exits 2 with a message on any validation failure.
  - `pool_list_json` — echoes `{"role":...,"entries":[{key,label,harness,default}]}`. No `model` field.
  - `pool_get <key>` — sets `ENTRY_KEY`, `ENTRY_LABEL`, `ENTRY_HARNESS`, `ENTRY_MODEL`. Exits 2 if the key is absent.
  - `pool_file_for <role>` — echoes the default pool path for `reviewer`/`reviewers` or `coder`/`coders`.
  - Env overrides `CSC_REVIEWERS_JSON` and `CSC_CODERS_JSON` for tests.

- [ ] **Step 1: Write the failing test**

Create `tests/test-pool.sh`:

```bash
#!/usr/bin/env bash
# Unit tests for scripts/lib/pool.sh and scripts/pool.sh. Run: bash tests/test-pool.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$HERE/../scripts/pool.sh"

PASS=0
FAIL=0
check() {  # check <description> <expected> <actual>
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A harness dir the fixtures can point at, so "harness file must exist" passes.
mkdir -p "$HERE/../scripts/harness"

write() {  # write <path> <json>
  printf '%s\n' "$2" > "$TMP/$1"
}

# rc_of <fixture> -> exit code of `pool.sh list reviewers` against it
rc_of() {
  CSC_REVIEWERS_JSON="$TMP/$1" bash "$CLI" list reviewers >/dev/null 2>&1
  echo $?
}

GOOD='{"reviewers":[
  {"key":"a-one","label":"A One","harness":"codex","model":"m-1","default":true},
  {"key":"b-two","label":"B Two","harness":"cursor","model":"m.2"}]}'
write good.json "$GOOD"

out=$(CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" list reviewers 2>/dev/null)
check "valid pool exits 0"            "0" "$(rc_of good.json)"
check "role echoed"                   "reviewers" "$(echo "$out" | jq -r '.role')"
check "two entries"                   "2" "$(echo "$out" | jq '.entries|length')"
check "file order preserved"          "a-one b-two" "$(echo "$out" | jq -r '[.entries[].key]|join(" ")')"
check "default flag carried"          "true" "$(echo "$out" | jq -r '.entries[0].default')"
check "listing has no model field"    "0" "$(echo "$out" | jq '[.entries[]|has("model")]|map(select(.))|length')"
check "single JSON line on stdout"    "1" "$(echo "$out" | wc -l | tr -d ' ')"

# --- rejections: each must exit 2 ---
write not-json.json 'this is not json'
write wrong-key.json '{"coders":[{"key":"a","label":"A","harness":"codex","model":"m"}]}'
write not-array.json '{"reviewers":{}}'
write empty.json '{"reviewers":[]}'
write not-object.json '{"reviewers":["a"]}'
write extra-key.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m","lable":"typo"}]}'
write no-label.json '{"reviewers":[{"key":"a","harness":"codex","model":"m"}]}'
write bad-key.json '{"reviewers":[{"key":"Bad Key","label":"A","harness":"codex","model":"m"}]}'
write bad-model.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"bad model"}]}'
write bad-harness.json '{"reviewers":[{"key":"a","label":"A","harness":"Codex","model":"m"}]}'
write traversal.json '{"reviewers":[{"key":"a","label":"A","harness":"../../etc","model":"m"}]}'
write absent-harness.json '{"reviewers":[{"key":"a","label":"A","harness":"nosuchtool","model":"m"}]}'
write dup-key.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m"},{"key":"a","label":"B","harness":"codex","model":"m"}]}'
write two-defaults.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m","default":true},{"key":"b","label":"B","harness":"codex","model":"m","default":true}]}'
write bad-default.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m","default":"yes"}]}'
write newline-label.json '{"reviewers":[{"key":"a","label":"A\nB","harness":"codex","model":"m"}]}'

for f in not-json wrong-key not-array empty not-object extra-key no-label bad-key \
         bad-model bad-harness traversal absent-harness dup-key two-defaults \
         bad-default newline-label; do
  check "rejects $f" "2" "$(rc_of "$f.json")"
done

check "missing file exits 2" "2" "$(rc_of does-not-exist.json)"

# --- lookup by key ---
out=$(CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" get reviewers b-two 2>/dev/null)
check "get returns the model"  "m.2"    "$(echo "$out" | jq -r '.model')"
check "get returns harness"    "cursor" "$(echo "$out" | jq -r '.harness')"

CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" get reviewers nope >/dev/null 2>&1
check "unknown key exits 2" "2" "$?"

err=$(CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" get reviewers nope 2>&1 >/dev/null)
check "unknown key lists valid keys" "1" "$([[ "$err" == *a-one* && "$err" == *b-two* ]] && echo 1 || echo 0)"

# --- the shipped pool files are themselves valid ---
check "shipped reviewers.json valid" "0" "$(bash "$CLI" list reviewers >/dev/null 2>&1; echo $?)"
check "shipped coders.json valid"    "0" "$(bash "$CLI" list coders   >/dev/null 2>&1; echo $?)"
check "shipped reviewers count"      "4" "$(bash "$CLI" list reviewers | jq '.entries|length')"
check "shipped coders count"         "2" "$(bash "$CLI" list coders   | jq '.entries|length')"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/test-pool.sh`
Expected: FAIL — `scripts/pool.sh` does not exist, so every case errors.

- [ ] **Step 3: Create the two pool files**

`.claude-plugin/reviewers.json`:

```json
{
  "reviewers": [
    { "key": "codex-sol",    "label": "Codex GPT-5.6 Sol",  "harness": "codex",  "model": "gpt-5.6-sol", "default": true },
    { "key": "cursor-grok",  "label": "Cursor Grok 4.6",    "harness": "cursor", "model": "cursor-grok-4.6-high" },
    { "key": "cursor-sol",   "label": "Cursor GPT-5.6 Sol", "harness": "cursor", "model": "gpt-5.6-sol-high" },
    { "key": "claude-opus5", "label": "Claude Opus 5",      "harness": "claude", "model": "claude-opus-5" }
  ]
}
```

`.claude-plugin/coders.json`:

```json
{
  "coders": [
    { "key": "cursor-composer", "label": "Cursor Composer 2.5", "harness": "cursor", "model": "composer-2.5", "default": true },
    { "key": "codex-luna",      "label": "Codex GPT-5.6 Luna",  "harness": "codex",  "model": "gpt-5.6-luna" }
  ]
}
```

- [ ] **Step 4: Write `scripts/lib/pool.sh`**

```bash
#!/usr/bin/env bash
# pool.sh (library) — read and validate a reviewer/coder pool file.
# Source this; do not execute it. Every function writes diagnostics to stderr only.

POOL_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POOL_PLUGIN_ROOT="$(cd "$POOL_LIB_DIR/../.." && pwd)"
POOL_HARNESS_DIR="$POOL_PLUGIN_ROOT/scripts/harness"

pool_die() { echo "error: $*" >&2; exit 2; }

# pool_file_for <role> -> echoes the pool file path for reviewer(s) or coder(s).
pool_file_for() {
  case "$1" in
    reviewer|reviewers) echo "${CSC_REVIEWERS_JSON:-$POOL_PLUGIN_ROOT/.claude-plugin/reviewers.json}" ;;
    coder|coders)       echo "${CSC_CODERS_JSON:-$POOL_PLUGIN_ROOT/.claude-plugin/coders.json}" ;;
    *) pool_die "unknown role '$1' (expected reviewer or coder)" ;;
  esac
}

# pool_role_key <role> -> the required top-level JSON key, always plural.
pool_role_key() {
  case "$1" in
    reviewer|reviewers) echo reviewers ;;
    coder|coders)       echo coders ;;
    *) pool_die "unknown role '$1'" ;;
  esac
}

# pool_load <file> <role> -> validates fully, sets POOL_JSON and POOL_ROLE_KEY.
# Every failure exits 2 with a message naming the entry and the field.
pool_load() {
  local file="$1" role="$2" key n i
  key="$(pool_role_key "$role")"
  POOL_ROLE_KEY="$key"

  [[ -r "$file" ]] || pool_die "pool file missing or unreadable: $file"
  jq -e . "$file" >/dev/null 2>&1 || pool_die "pool file is not valid JSON: $file"

  [[ "$(jq -r 'type' "$file")" == "object" ]] \
    || pool_die "$file: top level must be a JSON object"
  [[ "$(jq -r --arg k "$key" '[keys[]|select(. != $k)]|length' "$file")" == "0" ]] \
    && [[ "$(jq -r --arg k "$key" 'has($k)' "$file")" == "true" ]] \
    || pool_die "$file: top level must have exactly one key, \"$key\""
  [[ "$(jq -r --arg k "$key" '.[$k]|type' "$file")" == "array" ]] \
    || pool_die "$file: .$key must be an array"

  n="$(jq -r --arg k "$key" '.[$k]|length' "$file")"
  [[ "$n" -gt 0 ]] || pool_die "$file: .$key must not be empty"

  local seen_keys="" defaults=0
  for (( i=0; i<n; i++ )); do
    local e ekey elabel eharness emodel edefault extra
    e="$(jq -c --arg k "$key" --argjson i "$i" '.[$k][$i]' "$file")"
    [[ "$(jq -r 'type' <<<"$e")" == "object" ]] \
      || pool_die "$file: entry $i is not an object"

    extra="$(jq -r '[keys[]|select(. as $x | ["key","label","harness","model","default"]|index($x)|not)]|join(",")' <<<"$e")"
    [[ -z "$extra" ]] || pool_die "$file: entry $i has unknown field(s): $extra"

    for f in key label harness model; do
      [[ "$(jq -r --arg f "$f" 'has($f)' <<<"$e")" == "true" ]] \
        || pool_die "$file: entry $i is missing required field '$f'"
      [[ "$(jq -r --arg f "$f" '.[$f]|type' <<<"$e")" == "string" ]] \
        || pool_die "$file: entry $i field '$f' must be a string"
    done

    ekey="$(jq -r '.key' <<<"$e")"
    elabel="$(jq -r '.label' <<<"$e")"
    eharness="$(jq -r '.harness' <<<"$e")"
    emodel="$(jq -r '.model' <<<"$e")"

    [[ "$ekey"     =~ ^[a-z0-9][a-z0-9-]*$ ]]   || pool_die "$file: entry $i key '$ekey' must match ^[a-z0-9][a-z0-9-]*$"
    [[ "$eharness" =~ ^[a-z][a-z0-9-]*$ ]]      || pool_die "$file: entry $i harness '$eharness' must match ^[a-z][a-z0-9-]*$"
    [[ "$emodel"   =~ ^[A-Za-z0-9._+-]+$ ]]     || pool_die "$file: entry $i model '$emodel' must match ^[A-Za-z0-9._+-]+$"
    [[ -n "$elabel" ]]                          || pool_die "$file: entry $i label must not be empty"
    [[ "$elabel" != *$'\n'* ]]                  || pool_die "$file: entry $i label must not contain a newline"
    LC_ALL=C grep -q '[^ -~]' <<<"$elabel" \
      && pool_die "$file: entry $i label '$elabel' must be printable ASCII"

    [[ -r "$POOL_HARNESS_DIR/$eharness.sh" ]] \
      || pool_die "$file: entry $i harness file not found: $POOL_HARNESS_DIR/$eharness.sh"

    case " $seen_keys " in
      *" $ekey "*) pool_die "$file: duplicate key '$ekey'" ;;
    esac
    seen_keys="$seen_keys $ekey"

    if [[ "$(jq -r 'has("default")' <<<"$e")" == "true" ]]; then
      edefault="$(jq -r '.default|type' <<<"$e")"
      [[ "$edefault" == "boolean" ]] || pool_die "$file: entry $i field 'default' must be a boolean"
      [[ "$(jq -r '.default' <<<"$e")" == "true" ]] && defaults=$((defaults+1))
    fi
  done

  [[ "$defaults" -le 1 ]] || pool_die "$file: at most one entry may be marked default (found $defaults)"

  POOL_JSON="$(cat "$file")"
}

# pool_list_json -> one JSON line: role and entries, WITHOUT the model field.
# The model is withheld on purpose so a model name cannot reach the orchestrator.
pool_list_json() {
  jq -c --arg role "$POOL_ROLE_KEY" \
    '{role:$role, entries:[.[$role][]|{key,label,harness,default:(.default // false)}]}' \
    <<<"$POOL_JSON"
}

# pool_get <key> -> sets ENTRY_KEY/ENTRY_LABEL/ENTRY_HARNESS/ENTRY_MODEL.
pool_get() {
  local want="$1" e
  e="$(jq -c --arg role "$POOL_ROLE_KEY" --arg k "$want" \
        '.[$role][]|select(.key == $k)' <<<"$POOL_JSON")"
  if [[ -z "$e" ]]; then
    local valid
    valid="$(jq -r --arg role "$POOL_ROLE_KEY" '[.[$role][].key]|join(", ")' <<<"$POOL_JSON")"
    pool_die "unknown key '$want'. Valid keys: $valid"
  fi
  ENTRY_KEY="$(jq -r '.key' <<<"$e")"
  ENTRY_LABEL="$(jq -r '.label' <<<"$e")"
  ENTRY_HARNESS="$(jq -r '.harness' <<<"$e")"
  ENTRY_MODEL="$(jq -r '.model' <<<"$e")"
}
```

- [ ] **Step 5: Write `scripts/pool.sh`**

```bash
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
```

- [ ] **Step 6: Create placeholder harness files so validation can pass**

The pool validator requires `scripts/harness/<name>.sh` to exist. Task 3 fills these in; for now create empty files so Task 1's tests can pass.

```bash
mkdir -p scripts/harness
for h in cursor codex claude; do
  printf '#!/usr/bin/env bash\n# Filled in by Task 3.\n' > "scripts/harness/$h.sh"
done
chmod +x scripts/pool.sh
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `bash tests/test-pool.sh`
Expected: PASS, `FAIL=0`.

- [ ] **Step 8: Commit**

```bash
git add .claude-plugin/reviewers.json .claude-plugin/coders.json \
        scripts/lib/pool.sh scripts/pool.sh scripts/harness tests/test-pool.sh
git commit -m "feat: reviewer and coder pool files with a validating library"
```

---

### Task 2: Bounded execution helper

**Files:**
- Create: `scripts/lib/timeout.sh`
- Test: `tests/test-timeout.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: `run_with_timeout <seconds> <command...>` — runs the command in its own process group, returns the command's exit code, and sets `TIMEOUT_HIT` to `1` if it had to kill it, `0` otherwise.

- [ ] **Step 1: Write the failing test**

Create `tests/test-timeout.sh`:

```bash
#!/usr/bin/env bash
# Unit tests for scripts/lib/timeout.sh. Run: bash tests/test-timeout.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib/timeout.sh
source "$HERE/../scripts/lib/timeout.sh"

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

# --- a fast command is untouched ---
run_with_timeout 10 true
check "fast command returns its own status" "0" "$?"
check "fast command does not set TIMEOUT_HIT" "0" "$TIMEOUT_HIT"

run_with_timeout 10 false
check "failing command returns its own status" "1" "$?"
check "failing command does not set TIMEOUT_HIT" "0" "$TIMEOUT_HIT"

# --- a slow command is killed ---
start=$SECONDS
run_with_timeout 1 sleep 30
rc=$?
elapsed=$(( SECONDS - start ))
check "slow command is terminated"        "1" "$([[ $rc -ne 0 ]] && echo 1 || echo 0)"
check "slow command sets TIMEOUT_HIT"     "1" "$TIMEOUT_HIT"
check "returns promptly, not after 30s"   "1" "$([[ $elapsed -lt 15 ]] && echo 1 || echo 0)"

# --- the whole process GROUP dies, not just the direct child ---
# The parent script spawns a grandchild and records its pid, then sleeps.
# After the timeout, that grandchild must be gone: killing only the parent
# would leave a tool still running and possibly still editing files.
cat > "$TMP/spawner.sh" <<'SPAWN'
#!/usr/bin/env bash
sleep 300 &
echo $! > "$1"
sleep 300
SPAWN
chmod +x "$TMP/spawner.sh"

run_with_timeout 1 "$TMP/spawner.sh" "$TMP/grandchild.pid"
sleep 1
gc=$(cat "$TMP/grandchild.pid" 2>/dev/null || echo "")
check "grandchild pid was recorded" "1" "$([[ -n "$gc" ]] && echo 1 || echo 0)"
check "grandchild is killed too"    "1" "$(kill -0 "$gc" 2>/dev/null && echo 0 || echo 1)"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/test-timeout.sh`
Expected: FAIL — `scripts/lib/timeout.sh` does not exist, so `source` errors.

- [ ] **Step 3: Write `scripts/lib/timeout.sh`**

```bash
#!/usr/bin/env bash
# timeout.sh (library) — bounded execution with process-group cleanup.
# Source this; do not execute it.
#
# macOS has no `timeout` program by default, so this is hand-rolled. It must
# kill the whole process GROUP: killing only the direct child would leave the
# underlying CLI running, and an edit-capable tool could keep changing files
# after the caller has already reported BLOCKED.

TIMEOUT_HIT=0

# run_with_timeout <seconds> <command...>
# Returns the command's exit status. Sets TIMEOUT_HIT=1 if it had to kill it.
run_with_timeout() {
  local secs="$1"; shift
  local flag pid watcher rc
  flag="$(mktemp)"
  TIMEOUT_HIT=0

  # `set -m` gives the background job its own process group, so a negative
  # pid in `kill` reaches the command and everything it spawned.
  set -m
  "$@" &
  pid=$!
  set +m

  {
    sleep "$secs"
    if kill -0 "$pid" 2>/dev/null; then
      echo 1 > "$flag"
      kill -TERM -"$pid" 2>/dev/null
      sleep 5
      kill -KILL -"$pid" 2>/dev/null
    fi
  } &
  watcher=$!

  # If the caller is interrupted, take the whole group down rather than
  # orphaning it.
  trap 'kill -KILL -'"$pid"' 2>/dev/null; kill '"$watcher"' 2>/dev/null' INT TERM

  wait "$pid"; rc=$?

  trap - INT TERM
  kill "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null

  [[ -s "$flag" ]] && TIMEOUT_HIT=1
  rm -f "$flag"
  return "$rc"
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash tests/test-timeout.sh`
Expected: PASS, `FAIL=0`.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/timeout.sh tests/test-timeout.sh
git commit -m "feat: portable bounded execution helper with process-group cleanup"
```

---

### Task 3: Harness layer and `probe.sh`

**Files:**
- Modify: `scripts/harness/cursor.sh`, `scripts/harness/codex.sh`, `scripts/harness/claude.sh` (created empty in Task 1)
- Create: `scripts/probe.sh`, `tests/mock-claude`
- Test: `tests/test-probe.sh`

**Interfaces:**
- Consumes: `run_with_timeout` (Task 2); `pool_load`, `pool_file_for`, `pool_get` (Task 1).
- Produces, in every harness file:
  - `harness_probe <model>` — returns 0 when the tool replies `READY`. Sets `PROBE_REASON` to one of `auth`, `trust`, `not-installed`, `bad-model`, `timeout`, `other`. Writes the tool's error to stderr.
  - `harness_run <mode> <model> <dir> <prompt> <session>` — `mode` is `edit` or `read-only`; `dir` is absolute. Sets `SESSION_ID` and `RESULT`, returns 0 on success. **Must be called as a plain statement.**
  - `harness_render <json-line>` — one progress line to stderr.
  - Each harness reads its binary from an env override: `CSC_CURSOR_BIN`, `CSC_CODEX_BIN`, `CSC_CLAUDE_BIN`.
- `scripts/probe.sh --role <reviewer|coder> --key <k>` prints
  `{"status":"READY|FAILED","role":...,"key":...,"reason":...,"diagnostic":...}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test-probe.sh`:

```bash
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

# A reply that is not READY is a failure, not a pass.
out=$(MOCK_RESULT="not ready" bash "$SCRIPT" --role reviewer --key c-codex 2>/dev/null)
check "non-READY reply fails" "FAILED" "$(echo "$out" | jq -r '.status')"

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

CSC_CURSOR_BIN=/no/such/binary bash "$SCRIPT" --role reviewer --key c-cursor >/dev/null 2>&1
check "missing binary exits non-zero" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"
out=$(CSC_CURSOR_BIN=/no/such/binary bash "$SCRIPT" --role reviewer --key c-cursor 2>/dev/null)
check "missing binary classified" "not-installed" "$(echo "$out" | jq -r '.reason')"

# A failed probe must exit non-zero so callers can stop.
MOCK_RESULT="nope" bash "$SCRIPT" --role reviewer --key c-codex >/dev/null 2>&1
check "failed probe exits non-zero" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"

bash "$SCRIPT" --role reviewer --key nosuch >/dev/null 2>&1
check "unknown key exits 2" "2" "$?"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/test-probe.sh`
Expected: FAIL — `scripts/probe.sh` and `tests/mock-claude` do not exist.

- [ ] **Step 3: Write `tests/mock-claude`**

```bash
#!/usr/bin/env bash
# Mock of `claude` for harness tests. Mimics `claude -p --output-format stream-json`:
#   - prints newline-delimited JSON events to stdout
#   - exits 0 on success, 1 on simulated CLI error
# Controls (env vars):
#   MOCK_FAIL_CLI=1     -> print a plain-text error to stderr, exit 1
#   MOCK_STDERR=<text>  -> also write this text to stderr
#   MOCK_IS_ERROR=1     -> emit a result event with is_error:true, exit 0
#   MOCK_SESSION=<id>   -> session_id to emit (default claude-mock-1)
#   MOCK_RESULT=<text>  -> result text to emit (default "done"; empty stays empty)
#   MOCK_STREAM=1       -> emit progress events before the terminal result line
#   MOCK_LOG=<path>     -> append "ARGS: $*" (all argv, incl. the prompt) to this file
set -euo pipefail

if [[ -n "${MOCK_LOG:-}" ]]; then echo "ARGS: $*" >> "$MOCK_LOG"; fi
if [[ -n "${MOCK_STDERR:-}" ]]; then echo "$MOCK_STDERR" >&2; fi
if [[ "${MOCK_FAIL_CLI:-0}" == "1" ]]; then
  echo "claude: simulated CLI failure" >&2
  exit 1
fi

session="${MOCK_SESSION:-claude-mock-1}"
result="${MOCK_RESULT-done}"

printf '{"type":"system","subtype":"init","session_id":"%s","model":"mock"}\n' "$session"

if [[ "${MOCK_STREAM:-0}" == "1" ]]; then
  printf '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"working"}]},"session_id":"%s"}\n' "$session"
fi

if [[ "${MOCK_IS_ERROR:-0}" == "1" ]]; then
  printf '{"type":"result","subtype":"error","is_error":true,"result":"%s","session_id":"%s"}\n' "$result" "$session"
  exit 0
fi

printf '{"type":"result","subtype":"success","is_error":false,"result":"%s","session_id":"%s"}\n' "$result" "$session"
exit 0
```

Then: `chmod +x tests/mock-claude`

- [ ] **Step 4: Write `scripts/harness/cursor.sh`**

```bash
#!/usr/bin/env bash
# cursor.sh — how to drive `cursor-agent`. Sourced by the delegate scripts.
# Writes nothing to stdout. See the spec's "Calling rules" section.

CSC_CURSOR_BIN="${CSC_CURSOR_BIN:-cursor-agent}"

harness_render() {  # <json-line>
  local line="$1" type sub tool path text
  type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null) || return 0
  case "$type" in
    assistant)
      text=$(jq -r '.message.content[]? | select(.type=="text") | .text' <<<"$line" 2>/dev/null)
      [[ -n "$text" ]] && echo "  . $text" >&2
      ;;
    tool_call)
      sub=$(jq -r '.subtype // ""' <<<"$line" 2>/dev/null)
      tool=$(jq -r '.tool_call | keys[0] // "tool"' <<<"$line" 2>/dev/null)
      path=$(jq -r '.tool_call[]?.args.path // ""' <<<"$line" 2>/dev/null)
      echo "  -> ${tool} ${sub}${path:+ ($path)}" >&2
      ;;
  esac
}

# harness_probe <model>
harness_probe() {
  local model="$1" out rc
  PROBE_REASON=""
  if ! command -v "$CSC_CURSOR_BIN" >/dev/null 2>&1 && [[ ! -x "$CSC_CURSOR_BIN" ]]; then
    PROBE_REASON="not-installed"; echo "cursor-agent not found: $CSC_CURSOR_BIN" >&2; return 1
  fi
  out=$(mktemp)
  run_with_timeout "${CSC_PROBE_TIMEOUT:-120}" \
    "$CSC_CURSOR_BIN" -p --force --trust --mode ask --model "$model" \
    "Reply with the single word READY." >"$out" 2>>"$ERR_FILE"
  rc=$?
  if [[ "$TIMEOUT_HIT" == "1" ]]; then PROBE_REASON="timeout"; rm -f "$out"; return 1; fi
  if [[ $rc -ne 0 ]]; then
    PROBE_REASON="$(harness_classify "$(cat "$ERR_FILE" 2>/dev/null)")"
    rm -f "$out"; return 1
  fi
  if ! grep -q "READY" "$out"; then
    PROBE_REASON="other"; cat "$out" >> "$ERR_FILE"; rm -f "$out"; return 1
  fi
  rm -f "$out"; return 0
}

# harness_run <mode> <model> <dir> <prompt> <session>
# Sets SESSION_ID and RESULT. Call as a plain statement, never in $(...).
harness_run() {
  local mode="$1" model="$2" dir="$3" prompt="$4" sess="$5"
  local outfile rc line result_line="" is_err sub
  : > "$ERR_FILE"
  outfile=$(mktemp)

  local -a cmd=("$CSC_CURSOR_BIN" -p --force --trust --approve-mcps
                --output-format stream-json --model "$model")
  [[ "$mode" == "read-only" ]] && cmd+=(--mode ask)
  [[ -n "$sess" ]] && cmd+=(--resume="$sess")
  cmd+=("$prompt")

  # cursor-agent has no working-folder flag, so enter $dir ourselves.
  ( cd "$dir" && run_with_timeout "${CSC_RUN_TIMEOUT:-1800}" "${cmd[@]}" ) \
    >"$outfile" 2>>"$ERR_FILE"
  rc=$?

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "$(jq -r '.type // ""' <<<"$line" 2>/dev/null)" == "result" ]]; then
      result_line="$line"
    else
      harness_render "$line"
    fi
  done < "$outfile"
  rm -f "$outfile"

  [[ $rc -ne 0 ]] && return 1
  [[ -z "$result_line" ]] && return 1
  jq -e . <<<"$result_line" >/dev/null 2>&1 || return 1

  is_err=$(jq -r '.is_error // false' <<<"$result_line")
  sub=$(jq -r '.subtype // ""' <<<"$result_line")
  if [[ "$is_err" == "true" || ( -n "$sub" && "$sub" != "success" ) ]]; then
    jq -r '.result // "cursor-agent reported is_error"' <<<"$result_line" > "$ERR_FILE"
    return 1
  fi

  SESSION_ID=$(jq -r '.session_id // ""' <<<"$result_line")
  RESULT=$(jq -r '.result // ""' <<<"$result_line")
  return 0
}
```

- [ ] **Step 5: Write `scripts/harness/codex.sh`**

```bash
#!/usr/bin/env bash
# codex.sh — how to drive the Codex CLI. Sourced by the delegate scripts.
# Writes nothing to stdout.

CSC_CODEX_BIN="${CSC_CODEX_BIN:-codex}"

harness_render() {  # <json-line>
  local line="$1" type itype text
  type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null) || return 0
  [[ "$type" == "item.completed" ]] || return 0
  itype=$(jq -r '.item.type // ""' <<<"$line" 2>/dev/null)
  [[ "$itype" == "agent_message" ]] && return 0
  text=$(jq -r '.item.text // .item.command // ""' <<<"$line" 2>/dev/null)
  echo "  -> ${itype}${text:+ ($text)}" >&2
}

harness_probe() {
  local model="$1" out rc
  PROBE_REASON=""
  if ! command -v "$CSC_CODEX_BIN" >/dev/null 2>&1 && [[ ! -x "$CSC_CODEX_BIN" ]]; then
    PROBE_REASON="not-installed"; echo "codex not found: $CSC_CODEX_BIN" >&2; return 1
  fi
  out=$(mktemp)
  run_with_timeout "${CSC_PROBE_TIMEOUT:-120}" \
    "$CSC_CODEX_BIN" exec --json -s read-only -m "$model" \
    "Reply with the single word READY." >"$out" 2>>"$ERR_FILE" </dev/null
  rc=$?
  if [[ "$TIMEOUT_HIT" == "1" ]]; then PROBE_REASON="timeout"; rm -f "$out"; return 1; fi
  if [[ $rc -ne 0 ]] || grep -q '"type":"turn.failed"' "$out"; then
    PROBE_REASON="$(harness_classify "$(cat "$ERR_FILE" 2>/dev/null; cat "$out")")"
    cat "$out" >> "$ERR_FILE"; rm -f "$out"; return 1
  fi
  if ! grep -q "READY" "$out"; then
    PROBE_REASON="other"; cat "$out" >> "$ERR_FILE"; rm -f "$out"; return 1
  fi
  rm -f "$out"; return 0
}

harness_run() {
  local mode="$1" model="$2" dir="$3" prompt="$4" sess="$5"
  local outfile rc line type itype turn_failed="" fail_msg=""
  : > "$ERR_FILE"
  outfile=$(mktemp)
  SESSION_ID=""
  RESULT=""

  local -a cmd
  if [[ -n "$sess" ]]; then
    # VERIFIED 2026-08-22: `codex exec resume` has NO -C and NO -s/--sandbox.
    # Without the -c override below, a resumed read-only call would fall back
    # to the user's config.toml sandbox and could gain WRITE access.
    cmd=("$CSC_CODEX_BIN" exec resume "$sess" --json -m "$model")
    [[ "$mode" == "read-only" ]] && cmd+=(-c 'sandbox_mode="read-only"')
  else
    cmd=("$CSC_CODEX_BIN" exec --json -C "$dir" -m "$model")
    [[ "$mode" == "read-only" ]] && cmd+=(-s read-only)
  fi
  cmd+=("$prompt")

  # resume has no -C, so enter $dir for both paths.
  ( cd "$dir" && run_with_timeout "${CSC_RUN_TIMEOUT:-1800}" "${cmd[@]}" </dev/null ) \
    >"$outfile" 2>>"$ERR_FILE"
  rc=$?

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null)
    case "$type" in
      thread.started)
        [[ -z "$SESSION_ID" ]] && SESSION_ID=$(jq -r '.thread_id // ""' <<<"$line" 2>/dev/null)
        ;;
      item.completed)
        itype=$(jq -r '.item.type // ""' <<<"$line" 2>/dev/null)
        if [[ "$itype" == "agent_message" ]]; then
          RESULT=$(jq -r '.item.text // ""' <<<"$line" 2>/dev/null)
        else
          harness_render "$line"
        fi
        ;;
      turn.failed)
        turn_failed=1
        fail_msg=$(jq -r '.error.message // "codex reported turn.failed"' <<<"$line" 2>/dev/null)
        ;;
    esac
  done < "$outfile"
  rm -f "$outfile"

  if [[ -n "$turn_failed" ]]; then echo "$fail_msg" > "$ERR_FILE"; return 1; fi
  [[ $rc -ne 0 ]] && return 1
  return 0
}
```

- [ ] **Step 6: Write `scripts/harness/claude.sh`**

```bash
#!/usr/bin/env bash
# claude.sh — how to drive the Claude CLI headlessly. Sourced by the delegates.
# This starts a SEPARATE `claude -p` process; it is not an in-session subagent,
# which is what makes it usable as an independent reviewer.
# Writes nothing to stdout.

CSC_CLAUDE_BIN="${CSC_CLAUDE_BIN:-claude}"

harness_render() {  # <json-line>
  local line="$1" type text
  type=$(jq -r '.type // ""' <<<"$line" 2>/dev/null) || return 0
  [[ "$type" == "assistant" ]] || return 0
  text=$(jq -r '.message.content[]? | select(.type=="text") | .text' <<<"$line" 2>/dev/null)
  [[ -n "$text" ]] && echo "  . $text" >&2
}

harness_probe() {
  local model="$1" out rc
  PROBE_REASON=""
  if ! command -v "$CSC_CLAUDE_BIN" >/dev/null 2>&1 && [[ ! -x "$CSC_CLAUDE_BIN" ]]; then
    PROBE_REASON="not-installed"; echo "claude not found: $CSC_CLAUDE_BIN" >&2; return 1
  fi
  out=$(mktemp)
  run_with_timeout "${CSC_PROBE_TIMEOUT:-120}" \
    "$CSC_CLAUDE_BIN" -p --model "$model" --output-format stream-json \
    --allowedTools "Read" --permission-mode dontAsk \
    "Reply with the single word READY." >"$out" 2>>"$ERR_FILE" </dev/null
  rc=$?
  if [[ "$TIMEOUT_HIT" == "1" ]]; then PROBE_REASON="timeout"; rm -f "$out"; return 1; fi
  if [[ $rc -ne 0 ]]; then
    PROBE_REASON="$(harness_classify "$(cat "$ERR_FILE" 2>/dev/null)")"
    rm -f "$out"; return 1
  fi
  if ! grep -q "READY" "$out"; then
    PROBE_REASON="other"; cat "$out" >> "$ERR_FILE"; rm -f "$out"; return 1
  fi
  rm -f "$out"; return 0
}

harness_run() {
  local mode="$1" model="$2" dir="$3" prompt="$4" sess="$5"
  local outfile rc line result_line="" is_err sub
  : > "$ERR_FILE"
  outfile=$(mktemp)

  local -a cmd=("$CSC_CLAUDE_BIN" -p --model "$model" --output-format stream-json)
  if [[ "$mode" == "read-only" ]]; then
    cmd+=(--allowedTools "Read Grep Glob" --permission-mode dontAsk)
  else
    cmd+=(--permission-mode acceptEdits)
  fi
  [[ -n "$sess" ]] && cmd+=(--resume "$sess")
  cmd+=("$prompt")

  # claude has no working-folder flag, so enter $dir ourselves.
  ( cd "$dir" && run_with_timeout "${CSC_RUN_TIMEOUT:-1800}" "${cmd[@]}" </dev/null ) \
    >"$outfile" 2>>"$ERR_FILE"
  rc=$?

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "$(jq -r '.type // ""' <<<"$line" 2>/dev/null)" == "result" ]]; then
      result_line="$line"
    else
      harness_render "$line"
    fi
  done < "$outfile"
  rm -f "$outfile"

  [[ $rc -ne 0 ]] && return 1
  [[ -z "$result_line" ]] && return 1
  jq -e . <<<"$result_line" >/dev/null 2>&1 || return 1

  is_err=$(jq -r '.is_error // false' <<<"$result_line")
  sub=$(jq -r '.subtype // ""' <<<"$result_line")
  if [[ "$is_err" == "true" || ( -n "$sub" && "$sub" != "success" ) ]]; then
    jq -r '.result // "claude reported is_error"' <<<"$result_line" > "$ERR_FILE"
    return 1
  fi

  SESSION_ID=$(jq -r '.session_id // ""' <<<"$result_line")
  RESULT=$(jq -r '.result // ""' <<<"$result_line")
  return 0
}
```

- [ ] **Step 7: Write `scripts/lib/harness.sh` (shared loader and classifier)**

```bash
#!/usr/bin/env bash
# harness.sh (library) — load a harness file and classify its failures.
# Source this; do not execute it.

HARNESS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$HARNESS_LIB_DIR/../harness" && pwd)"

# harness_load <name> — sources scripts/harness/<name>.sh.
# The name was already checked against ^[a-z][a-z0-9-]*$ by pool_load, so it
# cannot escape this directory.
harness_load() {
  local name="$1" file="$HARNESS_DIR/$1.sh"
  [[ -r "$file" ]] || { echo "error: harness file not found: $file" >&2; exit 2; }
  # shellcheck source=/dev/null
  source "$file"
  local fn
  for fn in harness_probe harness_run harness_render; do
    declare -F "$fn" >/dev/null \
      || { echo "error: harness '$name' does not define $fn" >&2; exit 2; }
  done
}

# harness_classify <error-text> -> one of auth|trust|not-installed|bad-model|other
# Used so a probe failure gives the right advice instead of always saying "log in".
harness_classify() {
  local t="$1"
  case "$t" in
    *"Authentication required"*|*"not logged in"*|*"Unauthorized"*|*"401"*|*"login"*) echo auth ;;
    *"Workspace Trust"*|*"not trusted"*|*"trust"*)                                     echo trust ;;
    *"command not found"*|*"No such file or directory"*)                               echo not-installed ;;
    *"Cannot use this model"*|*"unknown model"*|*"model not found"*|*"invalid model"*) echo bad-model ;;
    *)                                                                                  echo other ;;
  esac
}
```

- [ ] **Step 8: Write `scripts/probe.sh`**

```bash
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
```

Then: `chmod +x scripts/probe.sh`

- [ ] **Step 9: Run the test to verify it passes**

Run: `bash tests/test-probe.sh`
Expected: PASS, `FAIL=0`.

- [ ] **Step 10: Commit**

```bash
git add scripts/harness scripts/lib/harness.sh scripts/probe.sh \
        tests/mock-claude tests/test-probe.sh
git commit -m "feat: harness layer for cursor, codex and claude, plus probe.sh"
```

---

### Task 4: `review-delegate.sh`

**Files:**
- Create: `scripts/review-delegate.sh`
- Test: `tests/test-review-delegate.sh`

**Interfaces:**
- Consumes: `pool_load`/`pool_get` (Task 1), `run_with_timeout` (Task 2), `harness_load`/`harness_run` (Task 3).
- Produces: `review-delegate.sh --reviewer <key> --target spec|plan --doc-file <f> [--spec-file <f>] [--lenses <csv>] [--rubric-dir <d>] [--session <id>]`, printing
  `{"status":"REVIEWED|BLOCKED","reviewer":...,"session_id":...,"target":...,"lenses":[...],"report":...,"diagnostic":...}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test-review-delegate.sh`:

```bash
#!/usr/bin/env bash
# Unit tests for scripts/review-delegate.sh across all three harnesses.
# Run: bash tests/test-review-delegate.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/review-delegate.sh"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}
has() { check "$1" "1" "$([ "$(grep -c -- "$2" "$3")" -ge 1 ] && echo 1 || echo 0)"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export CSC_CURSOR_BIN="$HERE/mock-cursor-agent"
export CSC_CODEX_BIN="$HERE/mock-codex"
export CSC_CLAUDE_BIN="$HERE/mock-claude"

cat > "$TMP/reviewers.json" <<'EOF'
{"reviewers":[
  {"key":"r-cursor","label":"Cursor","harness":"cursor","model":"m-cursor"},
  {"key":"r-codex","label":"Codex","harness":"codex","model":"m-codex"},
  {"key":"r-claude","label":"Claude","harness":"claude","model":"m-claude"}]}
EOF
export CSC_REVIEWERS_JSON="$TMP/reviewers.json"

# Stub rubrics with sentinels so prompt assembly can be asserted.
RUBRICS="$TMP/rubrics"; mkdir -p "$RUBRICS"
echo "SPEC_RUBRIC_SENTINEL"   > "$RUBRICS/spec-review.md"
echo "PLAN_RUBRIC_SENTINEL"   > "$RUBRICS/plan-review.md"
echo "BACKEND_LENS_SENTINEL"  > "$RUBRICS/lens-backend.md"
echo "FRONTEND_LENS_SENTINEL" > "$RUBRICS/lens-frontend.md"
echo "UI_LENS_SENTINEL"       > "$RUBRICS/lens-ui.md"
echo "OUTPUT_FORMAT_SENTINEL" > "$RUBRICS/_output-format.md"

REPO=$(cd "$(mktemp -d)" && pwd -P)
(cd "$REPO" && git init -q && git commit -q --allow-empty -m init)
doc="$REPO/spec.md"; echo "# Some Spec" > "$doc"

run() { bash "$SCRIPT" --doc-file "$doc" --rubric-dir "$RUBRICS" "$@"; }

# --- happy path on every harness ---
for k in r-cursor r-codex r-claude; do
  out=$(MOCK_RESULT="the review" run --reviewer "$k" --target spec 2>/dev/null)
  check "$k reports REVIEWED"    "REVIEWED"   "$(echo "$out" | jq -r '.status')"
  check "$k echoes reviewer key" "$k"         "$(echo "$out" | jq -r '.reviewer')"
  check "$k returns the report"  "the review" "$(echo "$out" | jq -r '.report')"
  check "$k returns a session"   "1" \
    "$([[ -n "$(echo "$out" | jq -r '.session_id')" ]] && echo 1 || echo 0)"
  check "$k emits one JSON line" "1" "$(echo "$out" | wc -l | tr -d ' ')"
done

# --- prompt assembly ---
log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target spec --lenses backend,ui >/dev/null 2>&1
has "spec rubric included"    "SPEC_RUBRIC_SENTINEL"   "$log"
has "backend lens included"   "BACKEND_LENS_SENTINEL"  "$log"
has "ui lens included"        "UI_LENS_SENTINEL"       "$log"
has "output format included"  "OUTPUT_FORMAT_SENTINEL" "$log"
check "frontend lens absent"  "0" "$(grep -c -- 'FRONTEND_LENS_SENTINEL' "$log")"
check "plan rubric absent"    "0" "$(grep -c -- 'PLAN_RUBRIC_SENTINEL' "$log")"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target plan --spec-file "$doc" >/dev/null 2>&1
has "plan rubric included" "PLAN_RUBRIC_SENTINEL" "$log"
rm -f "$log"

# --- read-only enforcement, including on resume ---
log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target spec >/dev/null 2>&1
has "codex new call is read-only" -- "-s read-only" "$log"
rm -f "$log"

# The regression this guards: `codex exec resume` has no -s flag, so without
# the -c override a resumed review silently loses its read-only sandbox.
log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-codex --target spec --session prev-1 >/dev/null 2>&1
has "codex resume uses the resume subcommand" "resume prev-1" "$log"
has "codex resume forces read-only via -c"    'sandbox_mode="read-only"' "$log"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-cursor --target spec --session prev-2 >/dev/null 2>&1
has "cursor resume keeps --mode ask" -- "--mode ask" "$log"
has "cursor resume passes the session" "resume=prev-2" "$log"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --reviewer r-claude --target spec --session prev-3 >/dev/null 2>&1
has "claude resume stays read-only" "dontAsk" "$log"
rm -f "$log"

# --- failures ---
out=$(MOCK_FAIL_CLI=1 run --reviewer r-codex --target spec 2>/dev/null)
check "tool failure is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "diagnostic is populated" "1" \
  "$([[ -n "$(echo "$out" | jq -r '.diagnostic')" ]] && echo 1 || echo 0)"

out=$(MOCK_RESULT="" run --reviewer r-codex --target spec 2>/dev/null)
check "empty report is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"

MOCK_FAIL_CLI=1 run --reviewer r-codex --target spec >/dev/null 2>&1
check "BLOCKED exits 1" "1" "$?"

# --- argument validation ---
bash "$SCRIPT" --rubric-dir "$RUBRICS" >/dev/null 2>&1
check "no args exits 2" "2" "$?"
run --reviewer r-codex --target bogus >/dev/null 2>&1
check "bad target exits 2" "2" "$?"
run --reviewer r-codex --target spec --lenses nope >/dev/null 2>&1
check "unknown lens exits 2" "2" "$?"
run --reviewer nosuch --target spec >/dev/null 2>&1
check "unknown reviewer exits 2" "2" "$?"
bash "$SCRIPT" --reviewer r-codex --target spec --doc-file /no/such --rubric-dir "$RUBRICS" >/dev/null 2>&1
check "missing doc exits 2" "2" "$?"
run --reviewer r-codex --target spec --rubric-dir /no/such >/dev/null 2>&1
check "missing rubric dir exits 2" "2" "$?"

rm -rf "$REPO"
echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/test-review-delegate.sh`
Expected: FAIL — `scripts/review-delegate.sh` does not exist.

- [ ] **Step 3: Write `scripts/review-delegate.sh`**

```bash
#!/usr/bin/env bash
# review-delegate.sh — delegate ONE design-spec or plan review to any reviewer
# from the pool, read-only, and return its report.
# Only the final STATUS JSON goes to stdout; progress and diagnostics to stderr.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pool.sh
source "$SCRIPT_DIR/lib/pool.sh"
# shellcheck source=lib/timeout.sh
source "$SCRIPT_DIR/lib/timeout.sh"
# shellcheck source=lib/harness.sh
source "$SCRIPT_DIR/lib/harness.sh"

usage() {
  echo "usage: review-delegate.sh --reviewer <key> --target spec|plan --doc-file <path>" >&2
  echo "       [--spec-file <path>] [--lenses backend,frontend,ui] [--rubric-dir <path>] [--session <id>]" >&2
}

REVIEWER=""; TARGET=""; DOC_FILE=""; SPEC_FILE=""; LENSES=""; SESSION=""
RUBRIC_DIR="$SCRIPT_DIR/../rubrics"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reviewer)   REVIEWER="${2:-}";   shift 2 ;;
    --target)     TARGET="${2:-}";     shift 2 ;;
    --doc-file)   DOC_FILE="${2:-}";   shift 2 ;;
    --spec-file)  SPEC_FILE="${2:-}";  shift 2 ;;
    --lenses)     LENSES="${2:-}";     shift 2 ;;
    --rubric-dir) RUBRIC_DIR="${2:-}"; shift 2 ;;
    --session)    SESSION="${2:-}";    shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$REVIEWER" ]] || { echo "error: --reviewer is required" >&2; usage; exit 2; }
[[ "$TARGET" == "spec" || "$TARGET" == "plan" ]] \
  || { echo "error: --target must be 'spec' or 'plan'" >&2; usage; exit 2; }
[[ -n "$DOC_FILE" && -r "$DOC_FILE" ]] \
  || { echo "error: --doc-file missing or unreadable" >&2; usage; exit 2; }
[[ -z "$SPEC_FILE" || -r "$SPEC_FILE" ]] \
  || { echo "error: --spec-file unreadable: $SPEC_FILE" >&2; exit 2; }

TARGET_RUBRIC="$RUBRIC_DIR/${TARGET}-review.md"
OUTPUT_FMT="$RUBRIC_DIR/_output-format.md"
for f in "$TARGET_RUBRIC" "$OUTPUT_FMT"; do
  [[ -r "$f" ]] || { echo "error: required rubric file missing: $f" >&2; exit 2; }
done

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
    [[ -r "$lf" ]] || { echo "error: lens rubric file missing: $lf" >&2; exit 2; }
    LENS_FILES+=("$lf")
  done
fi

pool_load "$(pool_file_for reviewer)" reviewer
pool_get "$REVIEWER"
harness_load "$ENTRY_HARNESS"

# Ground the reviewer in the document's own repository when it has one.
resolve_root() {
  local d; d="$(cd "$(dirname "$DOC_FILE")" && pwd)"
  git -C "$d" rev-parse --show-toplevel 2>/dev/null || echo "$d"
}
REPO_ROOT="$(resolve_root)"

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
  for lf in "${LENS_FILES[@]:-}"; do
    [[ -n "$lf" ]] || continue
    echo
    cat "$lf"
  done
  echo
  echo "Produce your review in EXACTLY the following format:"
  echo
  cat "$OUTPUT_FMT"
}

ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT
SESSION_ID=""
RESULT=""

emit() {  # emit <status> <session> <report> <diagnostic>
  jq -nc --arg status "$1" --arg reviewer "$REVIEWER" --arg session "$2" \
         --arg target "$TARGET" --arg lenses "$LENSES" \
         --arg report "$3" --arg diag "$4" \
    '{status:$status, reviewer:$reviewer, session_id:$session, target:$target,
      lenses:($lenses|split(",")|map(select(length>0))),
      report:$report, diagnostic:$diag}'
}

# harness_run is a PLAIN STATEMENT: it returns values in shell variables, which
# a $(...) or a pipeline would discard in a subshell.
if ! harness_run "read-only" "$ENTRY_MODEL" "$REPO_ROOT" "$(assemble_prompt)" "$SESSION"; then
  emit BLOCKED "$SESSION_ID" "" "reviewer invocation failed: $(cat "$ERR_FILE" 2>/dev/null)"
  exit 1
fi

if [[ -z "$RESULT" ]]; then
  emit BLOCKED "$SESSION_ID" "" "reviewer returned an empty review"
  exit 1
fi

emit REVIEWED "$SESSION_ID" "$RESULT" ""
exit 0
```

Then: `chmod +x scripts/review-delegate.sh`

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash tests/test-review-delegate.sh`
Expected: PASS, `FAIL=0`.

- [ ] **Step 5: Commit**

```bash
git add scripts/review-delegate.sh tests/test-review-delegate.sh
git commit -m "feat: generic review delegate over the reviewer pool"
```

---

### Task 5: `code-delegate.sh`

**Files:**
- Create: `scripts/code-delegate.sh`
- Test: `tests/test-code-delegate.sh`

**Interfaces:**
- Consumes: `pool_load`/`pool_get`, `run_with_timeout`, `harness_load`/`harness_run`.
- Produces: `code-delegate.sh --coder <key> --task-file <f> --verify-cmd <c> --cwd <dir> [--max-retries N] [--session <id>]`, printing
  `{"status":"DONE|BLOCKED","coder":...,"session_id":...,"attempts":N,"verified":bool,"changed":bool,"commit_id":"","result":...,"verify_output":...}`.
  `commit_id` is always an empty string here; the delegator subagent fills it in its own report after committing.

- [ ] **Step 1: Write the failing test**

Create `tests/test-code-delegate.sh`:

```bash
#!/usr/bin/env bash
# Unit tests for scripts/code-delegate.sh across all three harnesses.
# Run: bash tests/test-code-delegate.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/code-delegate.sh"

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

cat > "$TMP/coders.json" <<'EOF'
{"coders":[
  {"key":"c-cursor","label":"Cursor","harness":"cursor","model":"m-cursor"},
  {"key":"c-codex","label":"Codex","harness":"codex","model":"m-codex"},
  {"key":"c-claude","label":"Claude","harness":"claude","model":"m-claude"}]}
EOF
export CSC_CODERS_JSON="$TMP/coders.json"

# The worktree the coder is supposed to work in.
WT=$(cd "$(mktemp -d)" && pwd -P)
(cd "$WT" && git init -q && git commit -q --allow-empty -m init)
task="$TMP/task.md"; echo "Do the thing." > "$task"

run() { bash "$SCRIPT" --task-file "$task" --cwd "$WT" "$@"; }

# --- happy path on every harness ---
for k in c-cursor c-codex c-claude; do
  out=$(MOCK_RESULT="did it" run --coder "$k" --verify-cmd "true" 2>/dev/null)
  check "$k reports DONE"        "DONE"   "$(echo "$out" | jq -r '.status')"
  check "$k echoes coder key"    "$k"     "$(echo "$out" | jq -r '.coder')"
  check "$k verified true"       "true"   "$(echo "$out" | jq -r '.verified')"
  check "$k zero attempts"       "0"      "$(echo "$out" | jq -r '.attempts')"
  check "$k has commit_id field" "true"   "$(echo "$out" | jq 'has("commit_id")')"
  check "$k emits one JSON line" "1"      "$(echo "$out" | wc -l | tr -d ' ')"
done

# --- no verify command: trusted, but reported as unverified ---
out=$(run --coder c-codex --verify-cmd "" 2>/dev/null)
check "empty verify cmd is DONE"      "DONE"  "$(echo "$out" | jq -r '.status')"
check "empty verify cmd unverified"   "false" "$(echo "$out" | jq -r '.verified')"

# --- the verify command runs in --cwd, NOT in the caller's directory ---
# This is the single most likely porting bug: the old cc-delegate.sh ran
# `eval "$VERIFY_CMD"` wherever it happened to be invoked from.
out=$(cd "$TMP" && bash "$SCRIPT" --task-file "$task" --cwd "$WT" \
      --coder c-codex --verify-cmd "pwd > $TMP/verify-cwd.txt" 2>/dev/null)
check "verify ran in the worktree" "$WT" "$(cat "$TMP/verify-cwd.txt" 2>/dev/null)"

# --- retry loop ---
cnt="$TMP/attempts"; echo 0 > "$cnt"
vc="n=\$(cat $cnt); n=\$((n+1)); echo \$n > $cnt; [ \$n -ge 3 ]"
out=$(run --coder c-codex --verify-cmd "$vc" --max-retries 3 2>/dev/null)
check "retries until verify passes" "DONE" "$(echo "$out" | jq -r '.status')"
check "reports the attempt count"   "2"    "$(echo "$out" | jq -r '.attempts')"

out=$(run --coder c-codex --verify-cmd "false" --max-retries 2 2>/dev/null)
check "exhausted retries is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"
check "exhausted retries counts up"  "2"       "$(echo "$out" | jq -r '.attempts')"
check "verify output is kept"        "1" \
  "$([[ -n "$(echo "$out" | jq -r '.verify_output')" ]] && echo 1 || echo 0)"

run --coder c-codex --verify-cmd "false" --max-retries 1 >/dev/null 2>&1
check "BLOCKED exits 1" "1" "$?"

# --- edit mode is requested, not read-only ---
log="$TMP/args.log"
MOCK_LOG="$log" run --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "codex coder is not read-only" "0" "$(grep -c -- '-s read-only' "$log")"
check "codex coder gets -C worktree" "1" "$(grep -c -- "-C $WT" "$log")"
rm -f "$log"

log="$TMP/args.log"
MOCK_LOG="$log" run --coder c-claude --verify-cmd "true" >/dev/null 2>&1
check "claude coder accepts edits" "1" "$(grep -c -- 'acceptEdits' "$log")"
rm -f "$log"

# --- failures ---
out=$(MOCK_FAIL_CLI=1 run --coder c-codex --verify-cmd "true" 2>/dev/null)
check "tool failure is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"

# A result with no session id means the tool never really ran.
out=$(MOCK_SESSION="" run --coder c-cursor --verify-cmd "true" 2>/dev/null)
check "empty session id is BLOCKED" "BLOCKED" "$(echo "$out" | jq -r '.status')"

# --- argument validation ---
bash "$SCRIPT" >/dev/null 2>&1
check "no args exits 2" "2" "$?"
bash "$SCRIPT" --task-file "$task" --cwd "$WT" --coder c-codex >/dev/null 2>&1
check "missing --verify-cmd exits 2" "2" "$?"
bash "$SCRIPT" --task-file /no/such --cwd "$WT" --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "missing task file exits 2" "2" "$?"
bash "$SCRIPT" --task-file "$task" --cwd /no/such --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "missing --cwd exits 2" "2" "$?"
bash "$SCRIPT" --task-file "$task" --cwd "$TMP" --coder c-codex --verify-cmd "true" >/dev/null 2>&1
check "--cwd outside a git repo exits 2" "2" "$?"
run --coder nosuch --verify-cmd "true" >/dev/null 2>&1
check "unknown coder exits 2" "2" "$?"

rm -rf "$WT"
echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/test-code-delegate.sh`
Expected: FAIL — `scripts/code-delegate.sh` does not exist.

- [ ] **Step 3: Write `scripts/code-delegate.sh`**

```bash
#!/usr/bin/env bash
# code-delegate.sh — delegate ONE task to any coder from the pool, inside a
# worktree, then verify and retry.
# Only the final STATUS JSON goes to stdout; progress and diagnostics to stderr.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pool.sh
source "$SCRIPT_DIR/lib/pool.sh"
# shellcheck source=lib/timeout.sh
source "$SCRIPT_DIR/lib/timeout.sh"
# shellcheck source=lib/harness.sh
source "$SCRIPT_DIR/lib/harness.sh"

usage() {
  echo "usage: code-delegate.sh --coder <key> --task-file <path> --verify-cmd <cmd> --cwd <dir>" >&2
  echo "       [--max-retries N] [--session <id>]" >&2
}

CODER=""; TASK_FILE=""; VERIFY_CMD=""; VERIFY_SET=0; CWD=""; MAX_RETRIES=3; SESSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --coder)       CODER="${2:-}";       shift 2 ;;
    --task-file)   TASK_FILE="${2:-}";   shift 2 ;;
    --verify-cmd)  VERIFY_CMD="${2:-}"; VERIFY_SET=1; shift 2 ;;
    --cwd)         CWD="${2:-}";         shift 2 ;;
    --max-retries) MAX_RETRIES="${2:-3}"; shift 2 ;;
    --session)     SESSION="${2:-}";     shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$CODER" ]] || { echo "error: --coder is required" >&2; usage; exit 2; }
[[ -n "$TASK_FILE" && -r "$TASK_FILE" ]] \
  || { echo "error: --task-file missing or unreadable" >&2; usage; exit 2; }
[[ "$VERIFY_SET" -eq 1 ]] \
  || { echo "error: --verify-cmd is required (use \"\" for no verification)" >&2; usage; exit 2; }
[[ -n "$CWD" && -d "$CWD" ]] \
  || { echo "error: --cwd missing or not a directory" >&2; usage; exit 2; }

# Resolve to an absolute path and require a real git worktree. Everything below
# runs here: the task, every retry, the verify command, and every git call.
CWD="$(cd "$CWD" && pwd -P)"
git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "error: --cwd is not inside a git worktree: $CWD" >&2; exit 2; }

pool_load "$(pool_file_for coder)" coder
pool_get "$CODER"
harness_load "$ENTRY_HARNESS"

ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT
SESSION_ID=""
RESULT=""

emit() {  # emit <status> <session> <attempts> <verified> <changed> <result> <verify_output>
  jq -nc --arg status "$1" --arg coder "$CODER" --arg session "$2" \
         --argjson attempts "$3" --argjson verified "$4" --argjson changed "$5" \
         --arg result "$6" --arg vout "$7" \
    '{status:$status, coder:$coder, session_id:$session, attempts:$attempts,
      verified:$verified, changed:$changed, commit_id:"",
      result:$result, verify_output:$vout}'
}

# Did the coder actually change anything in the worktree?
changed_flag() {
  if [[ -n "$(git -C "$CWD" status --porcelain 2>/dev/null)" ]]; then echo true; else echo false; fi
}

# Run the verify command INSIDE the worktree. The old cc-delegate.sh ran this
# in the caller's directory, which would test the wrong tree entirely.
run_verify() {
  ( cd "$CWD" && eval "$VERIFY_CMD" ) 2>&1
}

PROMPT="Read the file $TASK_FILE and implement the task it describes. Make all necessary code edits."

if ! harness_run "edit" "$ENTRY_MODEL" "$CWD" "$PROMPT" "$SESSION"; then
  emit BLOCKED "$SESSION_ID" 0 false false "" "coder invocation failed: $(cat "$ERR_FILE" 2>/dev/null)"
  exit 1
fi

# No session id means the tool never really ran, whatever else it printed.
if [[ -z "$SESSION_ID" ]]; then
  emit BLOCKED "" 0 false "$(changed_flag)" "$RESULT" "coder returned no session id"
  exit 1
fi

if [[ -z "$VERIFY_CMD" ]]; then
  emit DONE "$SESSION_ID" 0 false "$(changed_flag)" "$RESULT" ""
  exit 0
fi

attempts=0
verify_out="$(run_verify)"
if [[ $? -eq 0 ]]; then
  emit DONE "$SESSION_ID" "$attempts" true "$(changed_flag)" "$RESULT" "$verify_out"
  exit 0
fi

while [[ $attempts -lt $MAX_RETRIES ]]; do
  attempts=$((attempts+1))
  fix_prompt="The verification command failed with this output:

$verify_out

Fix the code so the verification passes. Make all necessary edits."
  if ! harness_run "edit" "$ENTRY_MODEL" "$CWD" "$fix_prompt" "$SESSION_ID"; then
    emit BLOCKED "$SESSION_ID" "$attempts" false "$(changed_flag)" "$RESULT" \
      "coder failed during fix attempt $attempts: $(cat "$ERR_FILE" 2>/dev/null)"
    exit 1
  fi
  verify_out="$(run_verify)"
  if [[ $? -eq 0 ]]; then
    emit DONE "$SESSION_ID" "$attempts" true "$(changed_flag)" "$RESULT" "$verify_out"
    exit 0
  fi
done

emit BLOCKED "$SESSION_ID" "$attempts" false "$(changed_flag)" "$RESULT" "$verify_out"
exit 1
```

Then: `chmod +x scripts/code-delegate.sh`

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash tests/test-code-delegate.sh`
Expected: PASS, `FAIL=0`.

- [ ] **Step 5: Commit**

```bash
git add scripts/code-delegate.sh tests/test-code-delegate.sh
git commit -m "feat: generic code delegate that works inside a worktree"
```

---

### Task 6: `worktree.sh`

**Files:**
- Create: `scripts/worktree.sh`
- Test: `tests/test-worktree.sh`, `tests/test-worktree-isolation.sh`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `worktree.sh prepare` → `{"status":"READY","worktree":...,"work_branch":...,"feature_branch":...,"copied":[...],"skipped":[...]}`
  - `worktree.sh remove` → `{"status":"REMOVED|REFUSED","work_branch":...,"unmerged":[...],"diagnostic":...}`
  - `prepare` writes the copied-folder manifest to `<worktree-git-dir>/csc-copied`, because `remove` is a separate invocation and cannot see `prepare`'s output.

- [ ] **Step 1: Write the failing test**

Create `tests/test-worktree.sh`:

```bash
#!/usr/bin/env bash
# Unit tests for scripts/worktree.sh against real temporary git repositories.
# Run: bash tests/test-worktree.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/worktree.sh"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

ROOT=$(cd "$(mktemp -d)" && pwd -P)
trap 'rm -rf "$ROOT"' EXIT

# new_repo <name> [branch] -> echoes the repo path, on the given branch.
new_repo() {
  local name="$1" branch="${2:-feature/login}" d="$ROOT/$1"
  mkdir -p "$d"
  git -C "$d" init -q -b main
  git -C "$d" config user.email t@example.com
  git -C "$d" config user.name Test
  echo hello > "$d/README.md"
  git -C "$d" add -A && git -C "$d" commit -q -m init
  [[ "$branch" != "main" ]] && git -C "$d" checkout -q -b "$branch"
  echo "$d"
}

# --- refusals before anything is created ---
r=$(new_repo r-main main)
(cd "$r" && bash "$SCRIPT" prepare >/dev/null 2>&1)
check "refuses on main" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"

r=$(new_repo r-work "feature/x-work")
(cd "$r" && bash "$SCRIPT" prepare >/dev/null 2>&1)
check "refuses on a -work branch" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"

# --- happy path ---
r=$(new_repo r-ok)
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null)
wt=$(echo "$out" | jq -r '.worktree')
check "prepare reports READY"        "READY"              "$(echo "$out" | jq -r '.status')"
check "work branch is named"         "feature/login-work" "$(echo "$out" | jq -r '.work_branch')"
check "feature branch is echoed"     "feature/login"      "$(echo "$out" | jq -r '.feature_branch')"
check "worktree folder exists"       "1"                  "$([[ -d "$wt" ]] && echo 1 || echo 0)"
check "worktree is a sibling"        "1" \
  "$([[ "$(dirname "$wt")" == "$(dirname "$r")" ]] && echo 1 || echo 0)"
check "slashes became dashes"        "1" \
  "$([[ "$(basename "$wt")" == *feature-login-work ]] && echo 1 || echo 0)"
check "worktree is on the work branch" "feature/login-work" \
  "$(git -C "$wt" rev-parse --abbrev-ref HEAD)"
check "one JSON line" "1" "$(echo "$out" | wc -l | tr -d ' ')"

# --- prepare is safe to run twice ---
(cd "$r" && bash "$SCRIPT" prepare >/dev/null 2>&1)
check "second prepare succeeds" "0" "$?"

# --- reuse refusals ---
r=$(new_repo r-ahead)
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
echo change > "$wt/new.txt"
git -C "$wt" add -A && git -C "$wt" commit -q -m "work commit"
err=$(cd "$r" && bash "$SCRIPT" prepare 2>&1 >/dev/null); rc=$?
check "refuses reuse when work branch is ahead" "1" "$([[ $rc -ne 0 ]] && echo 1 || echo 0)"
check "names the extra commit" "1" \
  "$([[ "$err" == *"work commit"* ]] && echo 1 || echo 0)"

r=$(new_repo r-dirty)
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
echo dirt > "$wt/dirty.txt"
(cd "$r" && bash "$SCRIPT" prepare >/dev/null 2>&1)
check "refuses reuse when worktree is dirty" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"

r=$(new_repo r-branch-only)
git -C "$r" branch feature/login-work
(cd "$r" && bash "$SCRIPT" prepare >/dev/null 2>&1)
check "refuses when only the branch exists" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"

r=$(new_repo r-folder-taken)
mkdir -p "$ROOT/r-folder-taken-feature-login-work"
(cd "$r" && bash "$SCRIPT" prepare >/dev/null 2>&1)
check "refuses when the folder name is taken" "1" "$([[ $? -ne 0 ]] && echo 1 || echo 0)"

# --- dependency copying ---
r=$(new_repo r-deps)
printf 'node_modules/\nbuild-cache/\n.env\n' > "$r/.gitignore"
git -C "$r" add -A && git -C "$r" commit -q -m ignore
mkdir -p "$r/node_modules/pkg" && echo lib > "$r/node_modules/pkg/index.js"
mkdir -p "$r/build-cache" && echo junk > "$r/build-cache/j"
echo "SECRET=1" > "$r/.env"
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
check "node_modules was copied"          "1" "$([[ -f "$wt/node_modules/pkg/index.js" ]] && echo 1 || echo 0)"
check "node_modules is not a symlink"    "1" "$([[ ! -L "$wt/node_modules" ]] && echo 1 || echo 0)"
check "copied list names node_modules"   "1" \
  "$(echo "$out" | jq -r '.copied|index("node_modules")|if . == null then 0 else 1 end')"
check "an ignored folder off the list is skipped" "1" "$([[ ! -e "$wt/build-cache" ]] && echo 1 || echo 0)"
check ".env is never copied"             "1" "$([[ ! -e "$wt/.env" ]] && echo 1 || echo 0)"

# --- remove ---
r=$(new_repo r-remove)
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
echo change > "$wt/f.txt"
git -C "$wt" add -A && git -C "$wt" commit -q -m "task 1"
out=$(cd "$r" && bash "$SCRIPT" remove 2>/dev/null)
check "refuses to remove unmerged work" "REFUSED" "$(echo "$out" | jq -r '.status')"
check "lists the unmerged commit"       "1" \
  "$(echo "$out" | jq -r '[.unmerged[]|select(test("task 1"))]|length|if . > 0 then 1 else 0 end')"
check "worktree still exists"           "1" "$([[ -d "$wt" ]] && echo 1 || echo 0)"

git -C "$r" merge --ff-only feature/login-work -q
out=$(cd "$r" && bash "$SCRIPT" remove 2>/dev/null)
check "removes once merged"      "REMOVED" "$(echo "$out" | jq -r '.status')"
check "worktree folder is gone"  "1" "$([[ ! -d "$wt" ]] && echo 1 || echo 0)"
check "work branch is deleted"   "1" \
  "$(git -C "$r" rev-parse --verify feature/login-work >/dev/null 2>&1 && echo 0 || echo 1)"

# remove must delete the untracked copied deps itself: git worktree remove
# refuses to run while untracked files are present.
r=$(new_repo r-remove-deps)
printf 'node_modules/\n' > "$r/.gitignore"
git -C "$r" add -A && git -C "$r" commit -q -m ignore
mkdir -p "$r/node_modules" && echo lib > "$r/node_modules/x.js"
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
echo keepme > "$wt/untracked-user-file.txt"
out=$(cd "$r" && bash "$SCRIPT" remove 2>/dev/null)
check "removes despite copied deps" "REMOVED" "$(echo "$out" | jq -r '.status')"
check "main checkout node_modules survives" "1" "$([[ -f "$r/node_modules/x.js" ]] && echo 1 || echo 0)"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
```

Create `tests/test-worktree-isolation.sh`:

```bash
#!/usr/bin/env bash
# Proves success criterion 4: the coder cannot write into the main checkout.
# This is the guarantee two earlier drafts of the design got wrong by using
# symbolic links, so it is proved here rather than asserted in prose.
# Run: bash tests/test-worktree-isolation.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/worktree.sh"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

ROOT=$(cd "$(mktemp -d)" && pwd -P)
trap 'rm -rf "$ROOT"' EXIT

r="$ROOT/app"
mkdir -p "$r"
git -C "$r" init -q -b main
git -C "$r" config user.email t@example.com
git -C "$r" config user.name Test
printf 'node_modules/\n' > "$r/.gitignore"
echo hi > "$r/README.md"
git -C "$r" add -A && git -C "$r" commit -q -m init
git -C "$r" checkout -q -b feature/iso

mkdir -p "$r/node_modules/pkg"
echo "ORIGINAL" > "$r/node_modules/pkg/index.js"

out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null)
wt=$(echo "$out" | jq -r '.worktree')

check "dependency arrived in the worktree" "ORIGINAL" "$(cat "$wt/node_modules/pkg/index.js")"

# Nothing under the copied folder may be a link out of the worktree.
links=$(find "$wt/node_modules" -type l | wc -l | tr -d ' ')
check "no symlinks inside the copy" "0" "$links"
check "the folder itself is not a symlink" "1" "$([[ ! -L "$wt/node_modules" ]] && echo 1 || echo 0)"

# THE POINT: writing in the worktree must not reach the main checkout.
echo "MODIFIED BY CODER" > "$wt/node_modules/pkg/index.js"
check "main checkout file is untouched" "ORIGINAL" "$(cat "$r/node_modules/pkg/index.js")"
check "worktree file did change"        "MODIFIED BY CODER" "$(cat "$wt/node_modules/pkg/index.js")"

# Adding and deleting in the worktree must not reach the main checkout either.
echo new > "$wt/node_modules/pkg/added.js"
check "added file does not appear in main" "1" \
  "$([[ ! -e "$r/node_modules/pkg/added.js" ]] && echo 1 || echo 0)"
rm -f "$wt/node_modules/pkg/index.js"
check "deleting in the worktree does not delete in main" "1" \
  "$([[ -f "$r/node_modules/pkg/index.js" ]] && echo 1 || echo 0)"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
```

- [ ] **Step 2: Run both tests to verify they fail**

Run: `bash tests/test-worktree.sh; bash tests/test-worktree-isolation.sh`
Expected: FAIL for both — `scripts/worktree.sh` does not exist.

- [ ] **Step 3: Write `scripts/worktree.sh`**

```bash
#!/usr/bin/env bash
# worktree.sh — create and remove the coder's isolated worktree.
#   worktree.sh prepare   -> creates <feature>-work and a sibling worktree folder
#   worktree.sh remove    -> tears it down, refusing if work would be lost
# Only one line of JSON reaches stdout; diagnostics go to stderr.
set -uo pipefail

die() { echo "error: $*" >&2; exit 1; }

# Folders brought into the worktree, when present and ignored by git.
DEP_LIST=(node_modules .venv venv vendor .bundle target .gradle .m2 .tox .cargo)
# Never brought across, even if a name above were to match one of these.
DENY_GLOBS=('.env*' '*.pem' '*.key' '*.p12' 'credentials*' '.netrc' '.npmrc' '.aws' '.ssh')
# Above this many entries, a folder is skipped unless cloning is available.
BIG_DIR_ENTRIES=5000

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"
ROOT="$(cd "$ROOT" && pwd -P)"
FEATURE="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
WORK="${FEATURE}-work"
SLUG="${FEATURE//\//-}"
WT="$(dirname "$ROOT")/$(basename "$ROOT")-${SLUG}-work"

branch_exists() { git -C "$ROOT" rev-parse --verify --quiet "refs/heads/$1" >/dev/null; }

# Is $WT registered to this repository and checked out on $WORK?
wt_registered() {
  git -C "$ROOT" worktree list --porcelain \
    | awk -v p="$WT" -v b="refs/heads/$WORK" '
        /^worktree /   { cur = substr($0, 10) }
        /^branch /     { if (cur == p && substr($0, 8) == b) { found = 1 } }
        END            { exit found ? 0 : 1 }'
}

# Path of this worktree's private git directory, where the manifest lives.
wt_git_dir() { git -C "$WT" rev-parse --absolute-git-dir 2>/dev/null; }

clone_supported() {
  local probe rc
  probe="$(mktemp -d)"; mkdir -p "$probe/a"; : > "$probe/a/f"
  if cp -Rc "$probe/a" "$probe/b" 2>/dev/null || cp -R --reflink=always "$probe/a" "$probe/c" 2>/dev/null; then
    rc=0
  else
    rc=1
  fi
  rm -rf "$probe"
  return $rc
}

# clone_dir <src> <dst> — copy-on-write where possible, plain copy otherwise.
# NEVER a symbolic link: a link is a two-way door back into the main checkout.
clone_dir() {
  cp -Rc "$1" "$2" 2>/dev/null && return 0
  cp -R --reflink=auto "$1" "$2" 2>/dev/null && return 0
  cp -R "$1" "$2" 2>/dev/null && return 0
  return 1
}

denied() {
  local name="$1" g
  for g in "${DENY_GLOBS[@]}"; do
    # shellcheck disable=SC2053
    [[ "$name" == $g ]] && return 0
  done
  return 1
}

cmd_prepare() {
  [[ "$FEATURE" != "main" && "$FEATURE" != "master" ]] \
    || die "refusing to run on '$FEATURE'. Create a feature branch first."
  [[ "$FEATURE" != *-work ]] \
    || die "refusing to run on '$FEATURE': it is already a work branch."

  local have_branch=0 have_wt=0
  branch_exists "$WORK" && have_branch=1
  [[ -e "$WT" ]] && have_wt=1

  if [[ $have_branch -eq 1 && $have_wt -eq 1 ]]; then
    wt_registered || die "'$WT' exists but is not this repository's worktree for '$WORK'. Inspect it by hand."
    [[ -z "$(git -C "$WT" status --porcelain 2>/dev/null)" ]] \
      || die "worktree '$WT' has uncommitted changes from an earlier run. Commit, discard, or remove it."
    # Strict on purpose: a clean branch merely DESCENDED from the feature branch
    # may carry commits from an abandoned run, which this run's first
    # fast-forward would silently adopt as its own.
    if [[ "$(git -C "$ROOT" rev-parse "$WORK")" != "$(git -C "$ROOT" rev-parse "$FEATURE")" ]]; then
      echo "error: '$WORK' is not at the same commit as '$FEATURE'. Extra commits:" >&2
      git -C "$ROOT" log --oneline "$FEATURE..$WORK" >&2
      echo "Merge them deliberately or delete the branch, then run again." >&2
      exit 1
    fi
  elif [[ $have_branch -eq 1 || $have_wt -eq 1 ]]; then
    [[ $have_branch -eq 1 ]] \
      && die "branch '$WORK' exists but its worktree does not. Delete the branch or restore the worktree."
    die "'$WT' already exists but is not a worktree for '$WORK'. Move it aside."
  else
    git -C "$ROOT" worktree add -q -b "$WORK" "$WT" "$FEATURE" \
      || die "git worktree add failed"
  fi

  local copied=() skipped=() d entries
  local can_clone=0
  clone_supported && can_clone=1

  for d in "${DEP_LIST[@]}"; do
    [[ -e "$ROOT/$d" ]] || continue
    denied "$d" && continue
    git -C "$ROOT" check-ignore -q "$d" || continue
    [[ -e "$WT/$d" ]] && continue

    if [[ $can_clone -eq 0 ]]; then
      entries=$(find "$ROOT/$d" | head -n $((BIG_DIR_ENTRIES + 1)) | wc -l | tr -d ' ')
      if [[ "$entries" -gt "$BIG_DIR_ENTRIES" ]]; then
        skipped+=("$d"); continue
      fi
    fi

    if clone_dir "$ROOT/$d" "$WT/$d"; then
      copied+=("$d")
    else
      skipped+=("$d")
    fi
  done

  # remove runs later as a separate invocation, so persist what we created.
  local gd; gd="$(wt_git_dir)"
  if [[ -n "$gd" ]]; then
    printf '%s\n' "${copied[@]:-}" | grep -v '^$' > "$gd/csc-copied" || true
  fi

  jq -nc --arg wt "$WT" --arg work "$WORK" --arg feature "$FEATURE" \
         --arg copied "$(IFS=,; echo "${copied[*]:-}")" \
         --arg skipped "$(IFS=,; echo "${skipped[*]:-}")" \
    '{status:"READY", worktree:$wt, work_branch:$work, feature_branch:$feature,
      copied:($copied|split(",")|map(select(length>0))),
      skipped:($skipped|split(",")|map(select(length>0)))}'
}

cmd_remove() {
  branch_exists "$WORK" || {
    jq -nc --arg work "$WORK" \
      '{status:"REMOVED", work_branch:$work, unmerged:[], diagnostic:"nothing to remove"}'
    return 0
  }

  if ! git -C "$ROOT" merge-base --is-ancestor "$WORK" "$FEATURE"; then
    local unmerged
    unmerged="$(git -C "$ROOT" log --oneline "$FEATURE..$WORK" | jq -R . | jq -sc .)"
    jq -nc --arg work "$WORK" --argjson unmerged "$unmerged" \
      '{status:"REFUSED", work_branch:$work, unmerged:$unmerged,
        diagnostic:"work branch has commits not on the feature branch; nothing was deleted"}'
    return 1
  fi

  # Delete ONLY what prepare created. git worktree remove refuses to run while
  # untracked files are present, and the copied dependencies are untracked.
  local gd manifest d
  gd="$(wt_git_dir)"
  manifest="$gd/csc-copied"
  if [[ -r "$manifest" ]]; then
    while IFS= read -r d; do
      [[ -n "$d" ]] || continue
      rm -rf "${WT:?}/$d"
    done < "$manifest"
    rm -f "$manifest"
  fi

  git -C "$ROOT" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"
  git -C "$ROOT" worktree prune
  git -C "$ROOT" branch -D "$WORK" >/dev/null 2>&1

  jq -nc --arg work "$WORK" \
    '{status:"REMOVED", work_branch:$work, unmerged:[], diagnostic:""}'
}

case "${1:-}" in
  prepare) cmd_prepare ;;
  remove)  cmd_remove ;;
  *) echo "usage: worktree.sh prepare|remove" >&2; exit 2 ;;
esac
```

Then: `chmod +x scripts/worktree.sh`

- [ ] **Step 4: Run both tests to verify they pass**

Run: `bash tests/test-worktree.sh && bash tests/test-worktree-isolation.sh`
Expected: PASS for both, `FAIL=0`.

- [ ] **Step 5: Commit**

```bash
git add scripts/worktree.sh tests/test-worktree.sh tests/test-worktree-isolation.sh
git commit -m "feat: worktree lifecycle with copy-on-write dependency cloning"
```

---

### Task 7: The two delegator subagents

**Files:**
- Create: `agents/reviewer-delegator.md`, `agents/coder-delegator.md`
- Delete: `agents/cursor-coder-delegator.md`, `agents/cursor-reviewer-delegator.md`, `agents/codex-reviewer-delegator.md`

**Interfaces:**
- Consumes: `scripts/review-delegate.sh` (Task 4), `scripts/code-delegate.sh` (Task 5).
- Produces: two subagents that the Task 8 commands dispatch by name, `reviewer-delegator` and `coder-delegator`.

- [ ] **Step 1: Write `agents/reviewer-delegator.md`**

```markdown
---
name: reviewer-delegator
description: Delegates an independent design-spec or implementation-plan review to a reviewer chosen from the plugin's configured pool, running read-only, and relays the report verbatim. Use as the reviewer when an independent, unbiased review of a spec or plan is needed. Does not author, judge, or edit — it delegates and relays.
model: haiku
tools: Bash, Read
---

You are **reviewer-delegator**, a delegator. You do NOT review, judge, or write
anything yourself. You hand one review job to a reviewer chosen by the controller,
via a bundled script that runs that reviewer READ-ONLY, and you relay the result back
verbatim. The controller decides what to do with the findings.

**You have no authority to author, edit, or decide the validity of findings.** You
have no `Write`/`Edit` tools; do not attempt to modify any file by other means
(`Bash` redirection, `sed`, `tee`). You do not filter, reorder, soften, or embellish
the reviewer's report — you pass it through exactly. If the script cannot produce a
review, that is a **BLOCKED** outcome you report — never something you paper over by
writing your own review.

You are not told which model you are using and you do not need to know. The
controller gives you a **reviewer key**; the script resolves it.

## What you receive in your prompt
- The **reviewer key** — an opaque identifier the controller chose from the pool.
- The **target**: `spec` or `plan`.
- The **doc path**: the spec or plan file to review.
- Optionally, a **spec path** (for `plan` reviews — the spec the plan must satisfy).
- Optionally, **lenses** (for `spec` reviews — a comma-separated subset of
  `backend,frontend,ui`).
- Optionally, a **session id** to resume (for a follow-up review round).

## Your procedure
1. Run the delegate script (resolve its path via the plugin root):
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/review-delegate.sh" \
     --reviewer "<reviewer key>" \
     --target "<spec|plan>" \
     --doc-file "<doc path>" \
     [--spec-file "<spec path>" only for plan reviews when given] \
     [--lenses "<csv>" only for spec reviews when given] \
     [--session "<id>" only if you were given one to resume]
   ```
   You do NOT pass `--rubric-dir`; the script finds its bundled rubrics next to itself.
2. The script prints ONE line of JSON to stdout:
   `{"status":..., "reviewer":..., "session_id":..., "target":..., "lenses":..., "report":..., "diagnostic":...}`
   Read it with `jq`. Everything on stderr is progress and diagnostics.
3. Report back (see format). ALWAYS include the `session_id` verbatim so the
   controller can dispatch a follow-up round that resumes the reviewer's context.

## Report format
- **Status:** REVIEWED | BLOCKED
- **Reviewer key:** the script's `reviewer` field, copied verbatim.
- **Review:** the script's `report` field, reproduced VERBATIM and in full. Do not
  summarize, truncate, or edit it.
- **Reviewer session id:** the script's `session_id`, copied verbatim (REQUIRED).
- **On BLOCKED:** include the script's `diagnostic` so the controller can decide. An
  empty or missing `session_id` together with BLOCKED means the reviewer never ran a
  real session — report BLOCKED, never REVIEWED.

## Rules
- You never author, edit, or judge. Relaying is your only job.
- If the underlying tool is unauthenticated, untrusted, or unavailable, the script
  returns BLOCKED — report that and stop. Fixing the environment is the
  controller's or user's job.
- Keep stdout parsing strict: only the script's final JSON line matters.
```

- [ ] **Step 2: Write `agents/coder-delegator.md`**

```markdown
---
name: coder-delegator
description: Delegates a single implementation task to a coder chosen from the plugin's configured pool, running inside an isolated git worktree, verifies it, commits there, and reports back. Use as the implementer in subagent-driven development. Does not design, review, or write code itself — it delegates and verifies.
model: haiku
tools: Bash, Read
---

You are **coder-delegator**, a delegator. You do NOT write code yourself. You hand a
single task to a coder chosen by the controller (via a bundled script), verify the
result, commit it inside the worktree, and report back. The controller handles all
design and review.

**You have no authority to implement, edit, or fix source code — ever.** Your only
job is to invoke the delegate script and report exactly what it returns. You do not
have `Write`/`Edit` tools; do not attempt to write or patch source files by other
means (e.g. `Bash` redirection, `sed`, `tee`). The ONLY file you may create with
Bash is the temp task file in step 1. If the script cannot produce a verified
result, that is a **BLOCKED** outcome you report — never something you fix yourself.

You are not told which model you are using and you do not need to know. The
controller gives you a **coder key**; the script resolves it.

## What you receive in your prompt
- The **coder key** — an opaque identifier the controller chose from the pool.
- The **worktree path** — where the work happens. Everything you do happens here.
- The FULL TEXT of one task (already pasted in — do not go read a plan file).
- Scene-setting context (where it fits).
- A **verify command** — how to confirm the task works. If none is given, ask the
  controller for one; if it explicitly says "no verification", use an empty string.
- Optionally, a **session id** to resume (for fix-ups of prior work).

## Your procedure
1. Using **Bash** (you have no `Write` tool — and don't need one), create a temp
   file containing the task's full text via a quoted heredoc, so arbitrary
   code and quotes pass through unmodified:
   ```
   task_file=$(mktemp)
   cat > "$task_file" <<'__CC_TASK_EOF__'
   <the full task text exactly as given to you>
   __CC_TASK_EOF__
   ```
2. Run the delegate script:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/code-delegate.sh" \
     --coder "<coder key>" \
     --task-file "$task_file" \
     --verify-cmd "<the verify command, or empty string>" \
     --cwd "<the worktree path>" \
     --max-retries 3 \
     [--session "<id>" only if you were given one to resume]
   ```
3. The script prints ONE line of JSON to stdout:
   `{"status":..., "coder":..., "session_id":..., "attempts":N, "verified":bool, "changed":bool, "commit_id":"", "result":..., "verify_output":...}`
   Read it with `jq`. Everything on stderr is diagnostics.
4. If `status` is `DONE`, commit **inside the worktree**:
   1. Confirm the branch first:
      ```
      git -C "<worktree path>" rev-parse --abbrev-ref HEAD
      ```
      It MUST end in `-work`. If it does not, stop and report BLOCKED — you are not
      in the isolated worktree and must not commit.
   2. Check whether anything changed:
      ```
      git -C "<worktree path>" status --porcelain
      ```
      If the output is empty, the coder changed nothing. Report DONE, note that
      nothing changed, and make no commit. This is not an error.
   3. Otherwise commit and capture the id:
      ```
      git -C "<worktree path>" add -A
      git -C "<worktree path>" commit -m "<concise message describing the task>"
      git -C "<worktree path>" rev-parse HEAD
      ```
      If the commit fails, report BLOCKED with the git error. Never report DONE for
      a commit that did not happen.
5. Report back (see format below).

**Every git command you run must carry `-C "<worktree path>"`.** Without it you would
be acting on the controller's own checkout, which must never be touched.

## Report format (superpowers status protocol)
- **Status:** DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT
- **Coder key:** the script's `coder` field, copied verbatim.
- **What the coder did:** one or two sentences (from the script's `result`).
- **Verification:** the verify command and whether it passed (`verified`), in how
  many fix attempts (`attempts`).
- **Commit:** the commit id from step 4.3, or "no changes" when `changed` was false.
- **Files changed:** output of
  `git -C "<worktree path>" diff --name-only HEAD~1..HEAD`. If any changed file is
  OUTSIDE the set the task named, call it out explicitly so the controller can
  review scope.
- **Coder session id:** the script's `session_id`, copied verbatim (REQUIRED — for
  resume). Do not substitute your own agent id or "N/A".
- **Concerns:** anything notable.

Map the script result to status:
- script `DONE` + `verified:true` (or no verify cmd) → **DONE**
- script `BLOCKED` → **BLOCKED**, and include `verify_output` so the controller can decide.
- You were given no verify command and none could be obtained → **NEEDS_CONTEXT**.
- **Empty or missing `session_id`** → this is NOT a success: report **BLOCKED** with
  the diagnostic, even if files appear to have changed.

## Rules
- You never write, edit, or fix code. That is the coder's job via the script's
  resume loop. If the script returns BLOCKED, report BLOCKED with its diagnostic;
  do not hand-fix, do not improvise an implementation, and do not report DONE.
- If the underlying tool is unauthenticated, untrusted, or unavailable, the script
  returns BLOCKED — report that and stop.
- Never run a git command without `-C "<worktree path>"`.
- Keep stdout parsing strict: only the script's final JSON line matters.
```

- [ ] **Step 3: Delete the three old agent files**

```bash
git rm -q agents/cursor-coder-delegator.md \
          agents/cursor-reviewer-delegator.md \
          agents/codex-reviewer-delegator.md
```

- [ ] **Step 4: Verify no model name survives in `agents/`**

Run:
```bash
grep -rniE 'composer|grok|gpt-5|opus-5|sol|luna|claude-opus' agents/ || echo "clean"
```
Expected: `clean`. If anything matches, remove that wording — the agents must not name a model.

- [ ] **Step 5: Commit**

```bash
git add agents/reviewer-delegator.md agents/coder-delegator.md
git commit -m "feat: model-agnostic reviewer and coder delegator subagents"
```

---

### Task 8: The two commands, and the no-model-names guard

**Files:**
- Create: `commands/review.md`, `commands/implement-plans.md`
- Delete: `commands/cursor-review.md`, `commands/codex-review.md`, `commands/cursor-implement-plans.md`
- Test: `tests/test-no-model-names.sh`

**Interfaces:**
- Consumes: `scripts/pool.sh`, `scripts/probe.sh`, `scripts/worktree.sh`, and both subagents from Task 7.
- Produces: `/review` and `/implement-plans`.

- [ ] **Step 1: Write the failing test**

Create `tests/test-no-model-names.sh`:

```bash
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/test-no-model-names.sh`
Expected: FAIL — the old command files still exist and name models.

- [ ] **Step 3: Write `commands/review.md`**

```markdown
---
description: Get an independent review of a design spec or implementation plan from a reviewer you pick out of the configured pool, removing the bias of self-review. Usage: /review <spec|plan> <doc-path> [spec-path]
argument-hint: <spec|plan> <doc-path> [spec-path]
---

You are the **controller**. You will obtain an INDEPENDENT review of the document
named in `$ARGUMENTS` by delegating to a reviewer from the plugin's configured pool,
through the `reviewer-delegator` subagent. You do NOT review it yourself — that is the
point: the model that authored the document must not be the one that grades it.

You do not know or need to know which models are in the pool. You read the pool at
run time and let the user choose.

## Parse arguments
`$ARGUMENTS` is `<target> <doc-path> [spec-path]` where `target` is `spec` or `plan`.
- If `target` or `doc-path` is missing, ask the user for them and stop.
- `spec-path` is optional and used only for `plan` reviews (the spec the plan must satisfy).

## Preflight (do this first, stop on failure)

1. **Doc exists?** Confirm `doc-path` is a readable file. If not, tell the user and stop.

2. **Ask which reviewer.** Read the pool:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/pool.sh" list reviewers
   ```
   That prints `{"role":"reviewers","entries":[{"key","label","harness","default"}]}`.
   Build the menu from those entries, in the order given — never reorder them.
   - Four entries or fewer: use the pop-up menu, one option per entry, showing its
     `label`. Mark the entry whose `default` is true as recommended, in place.
   - More than four: print a numbered list and ask the user to type a number.

   Never write a model name into this file. The menu comes from the script.

3. **Probe the chosen reviewer for real.** A cached login is not proof:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/probe.sh" --role reviewer --key "<chosen key>"
   ```
   It prints `{"status":"READY"|"FAILED","reason":...,"diagnostic":...}`. On `FAILED`,
   give advice that matches `reason` and STOP:
   - `auth` — the tool is not logged in. Tell the user which tool and suggest they
     type `! <tool> login`.
   - `trust` — the workspace is not trusted by that tool.
   - `not-installed` — the tool is not on `PATH`.
   - `bad-model` — the model id in the pool file is wrong; point at
     `.claude-plugin/reviewers.json`.
   - `timeout` or `other` — show the `diagnostic` verbatim.

   Do not tell the user to log in unless `reason` is `auth`.

## Determine lenses (spec reviews only)

For `target spec`, decide which review lenses apply by reading the spec:
- **backend** — always include.
- **frontend** — include if the spec describes UI components, client state, routes,
  screens, or styling.
- **ui** — include if the spec describes user-facing flows, layouts, or UX.

If it is genuinely unclear whether the spec has a frontend or UI surface, ask the user
once. For `target plan`, do not pass lenses.

## Dispatch the reviewer

Dispatch the **`reviewer-delegator`** subagent (NOT a general-purpose subagent). Give it:
- the **reviewer key** the user chose,
- the `target` (`spec` or `plan`),
- the `doc-path`,
- the `spec-path` if this is a plan review,
- the comma-separated `lenses` if this is a spec review.

It shells to the read-only reviewer and returns the report verbatim, plus a
`session_id` and a `status` (REVIEWED | BLOCKED).

If it returns **BLOCKED**, surface the diagnostic to the user and stop — do not
fabricate a review or substitute your own.

## Act on the review (you, the controller)

Apply the **superpowers:receiving-code-review** skill to the returned report. Engage
each finding with technical rigor:
- Verify it against the document and the actual codebase before accepting it.
- Where the reviewer is right, plan or make the fix.
- Where the reviewer is wrong, push back with specific reasoning — do not perform
  agreement, and do not reflexively dismiss.

Present a triaged summary to the user (accepted / rejected-with-reason / needs-their-
decision). Offer a fix and re-review loop.

**On re-review, pass the prior `session_id`** so the reviewer resumes its own context
and can grade whether its earlier findings were actually addressed. A resumed review
is a follow-up, not a fresh delegation, so do NOT show the menu again — keep the same
reviewer.

## Final step: Review Effectiveness Summary (ALWAYS do this)

Produce a short **Review Effectiveness Summary** (about 10 to 15 lines). Do BOTH:
print it, and append it as a dated entry to `docs/cursor-reviewer/effectiveness-log.md`
in the working repo (create the dir and file if missing; if not writable or the user
objects, just print it and say where it would have gone). Capture:

- **Reviewer:** the `label` of the entry the user chose, and its key.
- **Run:** date, target, doc path, lenses used.
- **Findings:** count by severity (Critical / Important / Minor) and the verdict.
- **Triage outcome:** how many findings you accepted versus pushed back on, and why.
- **Reviewer quality:** were findings specific and codebase-grounded, or vague? Any
  false positives, or things it missed that you caught?
- **Environment friction:** probe failures, timeouts, BLOCKED, missing `session_id`.
- **Recommendations:** concrete changes to the rubrics or dispatch prompt that would
  improve the next review.

Base every line on what actually happened this run — do not invent metrics.
```

- [ ] **Step 4: Write `commands/implement-plans.md`**

```markdown
---
description: Implement a written plan by delegating each task to a coder you pick out of the configured pool, working in an isolated git worktree, while you review. Usage: /implement-plans <plan-path>
argument-hint: <path-to-plan-file>
---

You are the **controller**. You will implement the plan at `$ARGUMENTS` by delegating
each task's implementation to a coder from the plugin's configured pool (through the
`coder-delegator` subagent) while YOU do the planning extraction and all review. The
coder writes code; you never let it review.

You do not know or need to know which models are in the pool. You read the pool at run
time and let the user choose.

## Preflight (do this first, stop on failure)

1. **Plan path given?** If `$ARGUMENTS` is empty, ask the user for the plan file path
   and stop until provided.

2. **Branch safety.** Run `git rev-parse --abbrev-ref HEAD`. If it is `main` or
   `master`, tell the user and ask them to create a feature branch before you start.
   Do not implement on main or master.

3. **Clean working folder.** Run `git status --porcelain`. If there is any staged or
   unstaged change, stop and ask the user to commit or stash it. The worktree is
   created from the last commit, so uncommitted work would be invisible to the coder
   and would still be sitting in the way when the fast-forward happens later.

4. **Create the worktree.**
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh" prepare
   ```
   It prints `{"status":"READY","worktree":...,"work_branch":...,"feature_branch":...,"copied":[...],"skipped":[...]}`.
   Keep the `worktree` path — every coder dispatch needs it. If the command fails,
   show its error and stop; it refuses rather than guessing whenever the state is
   ambiguous. If `skipped` is not empty, warn the user that those dependency folders
   were not brought across, so a verify command may fail for a missing dependency.

5. **Load context.** Read the plan file at `$ARGUMENTS`. If it references a spec, read
   that too — you'll need it for spec-compliance review.

## Then: run subagent-driven development with the implementer overridden

Invoke the **superpowers:subagent-driven-development** skill and follow it, with these
FOUR overrides (state them to yourself before starting):

1. **Implementer = coder-delegator, working in the worktree.** For each task's
   implementation step:
   1. **Ask which coder**, unless the user already fixed one for this run:
      ```
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/pool.sh" list coders
      ```
      Build the menu from those entries, in the order given — never reorder them.
      Four entries or fewer: use the pop-up menu, showing each `label`, marking the
      `default` entry as recommended in place. More than four: print a numbered list.

      The **first** time you ask in a run, ask two things together: which coder, and
      whether to use it for the rest of the run. If the user says yes, do not show the
      menu again for the coder role.

      Never write a model name into this file. The menu comes from the script.
   2. **Probe it:**
      ```
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/probe.sh" --role coder --key "<chosen key>"
      ```
      On `FAILED`, give advice matching the `reason` field (`auth` → log in,
      `trust` → trust the workspace, `not-installed` → install the tool,
      `bad-model` → fix `.claude-plugin/coders.json`, otherwise show the
      `diagnostic`) and STOP. Do not say "log in" unless `reason` is `auth`.
   3. **Dispatch `coder-delegator`** (NOT a general-purpose subagent). Give it: the
      coder key, the **worktree path**, the task's full text, scene-setting context,
      and a concrete **verify command** for that task (derive it from the plan's
      verification steps; if a task has none, use the plan's general test command, or
      ask the user once).

2. **No interactive implementer Q&A.** The coder is one-shot; `coder-delegator` cannot
   hold a back-and-forth. If it returns **NEEDS_CONTEXT** or **BLOCKED**, treat it
   exactly as the skill says: provide more context and re-dispatch, break the task
   smaller, switch the verify command, or escalate to the user. Never silently retry
   unchanged.

3. **Reviews stay with you.** Keep the skill's two-stage review after each task — spec
   compliance first, then code quality — reading the change **in the worktree**. The
   coder is NEVER a reviewer, and reviewers from the pool are not used for task
   reviews.

   When a task needs changes, send your comments back to the **same coder session** by
   re-dispatching with the `session_id` the subagent returned. A fix is a follow-up,
   not a fresh delegation, so do NOT show the coder menu again.

4. **Fast-forward after each passing task.** Once your review passes, in the main
   folder run:
   ```
   git merge --ff-only <work-branch>
   ```
   This is what moves reviewed work onto the feature branch. If it fails, the feature
   branch has commits of its own and is no longer an ancestor of the work branch —
   most likely someone committed during the run. STOP and tell the user. Do not
   attempt a real merge; a conflict mid-run is worse than stopping. If the task
   changed nothing, this is a no-op and not an error.

Everything else about subagent-driven-development (serial dispatch, TodoWrite task
tracking, fix-and-re-review loops, handling DONE / DONE_WITH_CONCERNS) is UNCHANGED.

## When all tasks are done

1. **Tear down the worktree:**
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh" remove
   ```
   If it returns `REFUSED`, it found commits on the work branch that never reached the
   feature branch. Nothing was deleted. Show the `unmerged` list to the user and
   resolve it before continuing.

2. **Finish the branch.** Follow **superpowers:finishing-a-development-branch**. That
   skill verifies the tests pass, works out the base branch itself, and offers merge,
   push and open a pull request, keep the branch as-is, or discard. Do not reimplement
   any of that here.

## Final step: Delegation Effectiveness Summary (ALWAYS do this)

After `finishing-a-development-branch` completes — or if the run is abandoned partway —
produce a short **Delegation Effectiveness Summary**. This is the raw material for
improving the plugin, so it is required even when the run went well.

Do BOTH:
1. Print the summary to the user.
2. Append it as a dated entry to `docs/cursor-coder/effectiveness-log.md` in the
   working repo (create the dir and file if missing). If that repo is not writable or
   the user objects, just print it and say where it would have gone.

Keep it tight (about 10 to 15 lines). Capture:

- **Run:** date, plan path, branch, number of tasks delegated.
- **Coders used:** the `label` and key used for each task. If the user switched
  mid-run, say where and why.
- **Outcome:** how many tasks passed verification first try (`attempts:0`) versus
  needed fix loops, total fix attempts across the run, any `BLOCKED` or
  `NEEDS_CONTEXT` and the cause.
- **Coder fidelity:** did it follow the task specs? Note any out-of-scope file edits
  or autonomous commits you had to reconcile, and whether they were correct.
- **Worktree behaviour:** anything `prepare` skipped, verify failures caused by a
  missing dependency rather than by the code, and whether `remove` refused.
- **Environment friction:** probe failures, timeouts, missing `session_id` or resume
  failures, latency or attempt-count outliers.
- **Reliability flags (most important):** any case where `coder-delegator` did NOT
  actually delegate — it wrote code itself, reported `DONE` without a real
  `session_id`, committed outside the worktree, or otherwise bypassed the script.
  Call these out explicitly; they mean the delegation contract was broken.
- **Recommendations:** concrete changes to the plan format, dispatch prompts, the
  subagents, or the scripts that would make delegation smoother next time.

Base every line on what actually happened this run — do not invent metrics.
```

- [ ] **Step 5: Delete the three old command files**

```bash
git rm -q commands/cursor-review.md commands/codex-review.md commands/cursor-implement-plans.md
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `bash tests/test-no-model-names.sh`
Expected: PASS, `FAIL=0`.

- [ ] **Step 7: Commit**

```bash
git add commands/review.md commands/implement-plans.md tests/test-no-model-names.sh
git commit -m "feat: model-agnostic /review and /implement-plans commands"
```

---

### Task 9: Remove the old machinery and update the docs

**Files:**
- Delete: `scripts/sync-models.sh`, `scripts/cc-delegate.sh`, `scripts/cr-delegate.sh`, `scripts/cx-delegate.sh`, `.claude-plugin/models.json`, `tests/test-sync-models.sh`, `tests/test-drift-coverage.sh`, `tests/test-cc-delegate.sh`, `tests/test-cr-delegate.sh`, `tests/test-cx-delegate.sh`
- Modify: `Makefile`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `README.md`, `CHANGELOG.md`, `tests/e2e-smoke.md`

**Interfaces:**
- Consumes: everything from Tasks 1 to 8.
- Produces: a repository with no `models.json`, no marker comments, and a green test suite.

- [ ] **Step 1: Delete the superseded scripts and tests**

```bash
git rm -q scripts/sync-models.sh scripts/cc-delegate.sh scripts/cr-delegate.sh scripts/cx-delegate.sh
git rm -q .claude-plugin/models.json
git rm -q tests/test-sync-models.sh tests/test-drift-coverage.sh \
          tests/test-cc-delegate.sh tests/test-cr-delegate.sh tests/test-cx-delegate.sh
```

- [ ] **Step 2: Verify no marker comments or references survive**

Run:
```bash
grep -rn 'model:coder:label\|model:reviewer:label\|model:codex_reviewer:label\|models\.json\|sync-models' \
  --include='*.md' --include='*.sh' --include='*.json' . | grep -v '^./docs/superpowers/'
```
Expected: no output. The `docs/superpowers/` folder is excluded because the old specs
and plans are historical records and are left untouched.

If `README.md` or `tests/e2e-smoke.md` still match, they are rewritten in steps 5 and 7.

- [ ] **Step 3: Update the `Makefile`**

Replace the `models` target and remove the sync check from `release`.

```makefile
# Pretty-print the configured reviewer and coder pools.
models:
	@echo "reviewers:"; jq . $(REVIEWERS_JSON)
	@echo "coders:";    jq . $(CODERS_JSON)
```

Set the variables near `PLUGIN_JSON` and delete `MODELS_JSON`:

```makefile
REVIEWERS_JSON := .claude-plugin/reviewers.json
CODERS_JSON := .claude-plugin/coders.json
```

In the `release` target, delete this block entirely:

```makefile
	scripts/sync-models.sh --check || { \
	  echo "docs are stale relative to $(MODELS_JSON) — run scripts/sync-models.sh, review the diff, and commit first" >&2; \
	  exit 1; \
	}; \
```

Replace it with a test-suite gate, which is a better guard than the one it removes:

```makefile
	for t in tests/test-*.sh; do \
	  bash "$$t" >/dev/null || { echo "tests failing: $$t" >&2; exit 1; }; \
	done; \
```

- [ ] **Step 4: Update `plugin.json` and `marketplace.json`**

In `.claude-plugin/plugin.json`, set `version` to `2.0.0` and `description` to:

> Delegate implementation and independent design or plan review to a configurable pool of external models, run through Cursor, Codex or Claude, while Claude Code plans, decides and reviews.

In `.claude-plugin/marketplace.json`, set the plugin's `description` to:

> Delegation subagents over a configurable pool of coders and reviewers, run through Cursor, Codex or Claude.

- [ ] **Step 5: Rewrite `README.md`**

Rewrite it around the pools and the harness layer. It must:
- Describe the two pool files and show the entry shape (`key`, `label`, `harness`,
  `model`, `default`), pointing readers at `.claude-plugin/reviewers.json` and
  `.claude-plugin/coders.json` rather than listing models in prose.
- Document the two commands, `/review <spec|plan> <doc-path> [spec-path]` and
  `/implement-plans <plan-path>`, and say that each asks which worker to use.
- Explain the worktree: the coder works in `<feature>-work` in a sibling folder,
  dependency folders are copy-on-write clones so the main checkout is never written
  to, and reviewed work is fast-forwarded onto the feature branch after each task.
- Add a section "Adding a model" (one entry in one JSON file) and "Adding a tool"
  (one file in `scripts/harness/` defining `harness_probe`, `harness_run` and
  `harness_render`, plus a `tests/mock-<tool>` and pool entries).
- List the requirements: `cursor-agent`, `codex` and `claude` on `PATH` and logged in
  for whichever pool entries you intend to use; `jq`; bash 5; the `superpowers` plugin.
- Update the Tests section to the new file list.
- Contain **no** marker comments and **no** model names outside the pool examples.

- [ ] **Step 6: Add the `CHANGELOG.md` entry**

Add a `2.0.0` section at the top recording the breaking change:

```markdown
## 2.0.0

### Breaking
- The three tool-specific commands are removed and replaced by two:
  - `/cursor-review` and `/codex-review` → `/review`
  - `/cursor-implement-plans` → `/implement-plans`
  There are no aliases. Both new commands ask which worker to use.
- `.claude-plugin/models.json` is removed. Models now live in
  `.claude-plugin/reviewers.json` and `.claude-plugin/coders.json`.
- `scripts/sync-models.sh` and its two tests are removed. Nothing needs
  regenerating: the orchestrator reads the pool at run time.

### Added
- Configurable reviewer and coder pools; adding a model is one JSON entry.
- A harness layer (`scripts/harness/`), so adding a tool is one file with three
  functions. Claude joins Cursor and Codex as a supported tool.
- The coder now works in an isolated git worktree. Dependency folders are
  copy-on-write clones, so it cannot write into the main checkout.
- Bounded timeouts on every external call, terminating the whole process group.

### Fixed
- A resumed Codex review silently lost its read-only sandbox, because
  `codex exec resume` accepts neither `-C` nor `-s`. It now passes
  `-c sandbox_mode="read-only"`.
- The verify command ran in the caller's directory rather than the coder's.
```

- [ ] **Step 7: Rewrite `tests/e2e-smoke.md`**

Rewrite the manual end-to-end checks for the new commands. Cover, against real tools:
- `bash scripts/pool.sh list reviewers` and `list coders` return the shipped entries.
- `bash scripts/probe.sh --role reviewer --key <k>` returns `READY` for each of the
  four reviewers, and `--role coder` for both coders.
- `/review spec <a real spec>` end to end, once per reviewer.
- A re-review that passes `--session`, confirming the Codex path stays read-only.
- `/implement-plans` on a small throwaway plan: worktree created, dependency folders
  cloned, a task delegated and fast-forwarded, and the worktree removed.
- Remove every marker comment; name no model outside a pool-file quotation.

- [ ] **Step 8: Run the whole suite**

Run:
```bash
for t in tests/test-*.sh; do echo "== $t"; bash "$t" || echo "FAILED: $t"; done
```
Expected: every file ends with `PASS=n FAIL=0` and none prints `FAILED:`.

- [ ] **Step 9: Verify the plugin still loads**

Run: `claude plugin validate .`
Expected: `✔ Validation passed` (warnings are acceptable).

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat!: remove sync-models machinery and the tool-specific commands

BREAKING CHANGE: /cursor-review, /codex-review and /cursor-implement-plans
are replaced by /review and /implement-plans, with no aliases. models.json is
replaced by reviewers.json and coders.json."
```

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: pools and schema → Task 1; timeouts → Task 2; harness layer, resume safety and probe classification → Task 3; review delegate and rubric assembly → Task 4; code delegate and the `--cwd` contract → Task 5; worktree lifecycle, cloning and the isolation guarantee → Task 6; subagents and commit rules → Task 7; commands, menu semantics and the model-name guard → Task 8; deletions, `Makefile`, metadata, README, CHANGELOG and e2e → Task 9. All seven success criteria are covered: 1 and 2 by the pool and harness design plus the README section in Task 9; 3 by `test-no-model-names.sh`; 4 by `test-worktree-isolation.sh`; 5 by the rubric-sentinel assertions in Task 4; 6 by the resume assertions in Task 4; 7 by Task 9 step 8.

**Two additions the spec did not name.** `scripts/lib/harness.sh` was added to hold `harness_load` and `harness_classify`, which the spec implies but does not place; putting them in the three harness files would triplicate them. `tests/test-probe.sh` was added because the spec lists no test for `probe.sh`, yet probe failure classification is a named requirement.

**Type consistency.** `harness_probe`, `harness_run`, `harness_render`, `PROBE_REASON`, `SESSION_ID`, `RESULT`, `TIMEOUT_HIT`, `ERR_FILE`, `pool_load`, `pool_get`, `pool_file_for`, `pool_list_json`, `ENTRY_KEY`/`ENTRY_LABEL`/`ENTRY_HARNESS`/`ENTRY_MODEL`, and `harness_load` are spelled identically everywhere they appear. Every script that calls `harness_run` or `harness_probe` defines `ERR_FILE` first, because both write to it.

**One deliberate deviation from the spec.** The spec's `code-delegate.sh` output includes `commit_id`, but the script does not commit — the delegator subagent does. The field is present and always empty in the script's output, and the subagent reports the real value. The alternative, moving the commit into the script, would change who holds commit authority, which the spec assigns to the subagent.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-22-reviewer-coder-pools.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
