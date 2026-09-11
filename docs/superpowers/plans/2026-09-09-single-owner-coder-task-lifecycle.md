# Single-Owner Coder Task Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every delegated coding task exclusive ownership of its worktree until all writer processes have stopped, verification has finished, and the task result has been committed or safely blocked.

**Architecture:** `/implement-plans` will call `scripts/code-delegate.sh` synchronously instead of placing a shell-capable `coder-delegator` subagent between the controller and the external coder. A new lifecycle library will own a portable per-worktree lock and persistent recovery record. The existing timeout layer will prove process-group shutdown, while `code-delegate.sh` will validate separate verification commands, own Git commits, and emit the complete observed result.

**Tech Stack:** Bash 5, `jq`, Git, POSIX process groups, and the existing Cursor, Codex, and Claude CLI harnesses. Tests use plain Bash, deterministic fake CLIs, and temporary Git repositories.

**Spec:** `docs/superpowers/specs/2026-09-09-single-owner-coder-task-lifecycle-design.md`

## Global Constraints

- `/implement-plans` must never dispatch `coder-delegator` for implementation or fix rounds.
- One and only one lifecycle invocation may own a coder worktree at a time.
- `DONE` requires a stopped writer group, a real coder session id, successful or explicitly absent verification, and a clean worktree.
- A changed `DONE` result requires a commit id; a no-change `DONE` result requires an empty commit id.
- Timeout or failure must preserve partial files. No lifecycle path may stash, reset, clean, restore, or discard them.
- Uncertain process termination quarantines the worktree. Normal task admission cannot override quarantine.
- A dirty blocked task may be resumed only with its matching lifecycle id and coder session id.
- Recovery has no force mode and never deletes working files or commits.
- Every edit-capable coder and verification command is bounded. The default verification timeout is 1800 seconds through `CSC_VERIFY_TIMEOUT`.
- Supported harnesses must keep writer descendants in the process group created for the invocation.
- `--verify-cmd` may repeat. Each occurrence is one runnable command; an empty command may appear only by itself.
- A leading human label such as `server:` is invalid verification input.
- Every script continues to emit exactly one JSON object on stdout; progress and diagnostics go to stderr.
- The public coder menu, pools, worktree layout, review gates, fast-forward behavior, and reviewer workflow remain unchanged.
- Implementation must follow TDD: add the focused failing assertion, observe the expected failure, implement the smallest behavior, rerun the focused test, then run affected regression tests.

## File Structure

**Create**

| File | Responsibility |
|---|---|
| `scripts/lib/coder-lifecycle.sh` | Per-worktree ownership, recovery records, quarantine, and recovery validation. |
| `scripts/validate-code-result.sh` | Validate captured lifecycle JSON against its process exit status before the controller acts on it. |
| `tests/test-coder-lifecycle.sh` | Deterministic ownership, collision, recovery, and quarantine tests against real temporary worktrees. |
| `tests/test-code-result.sh` | Result schema, invariant, and exit-status contradiction tests. |

**Modify**

| File | Responsibility after this change |
|---|---|
| `scripts/lib/timeout.sh` | Bounded execution plus explicit process-group drain and containment state. |
| `scripts/harness/cursor.sh` | Run the Cursor CLI through the directory-aware timeout entry point and expose containment state. |
| `scripts/harness/codex.sh` | Run the Codex CLI through the directory-aware timeout entry point and expose containment state. |
| `scripts/harness/claude.sh` | Run the Claude CLI through the directory-aware timeout entry point and expose containment state. |
| `scripts/code-delegate.sh` | Entire coder lifecycle: input validation, ownership, coder execution, verification, retry, commit, recovery, and result JSON. |
| `commands/implement-plans.md` | Invoke the lifecycle directly, validate its result, review commits, and resume external coder sessions directly. |
| `tests/test-timeout.sh` | Normal-exit descendant, timeout, and uncontained-group regression coverage. |
| `tests/test-code-delegate.sh` | Multiple verification commands, exclusive ownership, recovery, Git ownership, and result-contract coverage. |
| `tests/mock-cursor-agent` | Deterministic file edit and lingering-child controls. |
| `tests/mock-codex` | Deterministic file edit and lingering-child controls. |
| `tests/mock-claude` | Deterministic file edit and lingering-child controls. |
| `tests/test-no-model-names.sh` | Assert the unsafe coder agent is absent and the command uses direct lifecycle execution. |
| `README.md` | Explain synchronous coder ownership and blocked-task recovery. |
| `tests/e2e-smoke.md` | Manual real-tool coverage of direct execution, concurrency refusal, timeout preservation, and recovery. |

**Delete**

| File | Reason |
|---|---|
| `agents/coder-delegator.md` | A shell-capable in-session subagent cannot be an enforceable task-completion boundary. |

---

### Task 1: Make process-group completion observable

**Files:**
- Modify: `scripts/lib/timeout.sh`
- Modify: `scripts/harness/cursor.sh`
- Modify: `scripts/harness/codex.sh`
- Modify: `scripts/harness/claude.sh`
- Test: `tests/test-timeout.sh`
- Test: `tests/test-code-delegate.sh`
- Test: `tests/test-review-delegate.sh`
- Test: `tests/test-probe.sh`

**Interfaces:**
- Consumes: existing `run_with_timeout <seconds> <command...>` calls.
- Produces:
  - `run_with_timeout_in <seconds> <directory> <command...>` — runs the command in `directory` without putting the timeout helper itself in a subshell.
  - `TIMEOUT_HIT` — `1` only when the deadline fired.
  - `RUN_GROUP_STOPPED` — `1` only when no member of the launched process group remains.
  - `RUN_GROUP_LINGERED` — `1` when the direct child exited but descendants remained and had to be terminated.
  - `RUN_GROUP_ID` — the process-group id used for diagnostics and lifecycle metadata.
  - Exit `125` when a lingering group was contained after an otherwise normal command exit.
  - Exit `126` when the helper cannot prove the process group stopped.
  - Existing exit `124` for a contained timeout; a command that independently exits 124 remains distinguishable through `TIMEOUT_HIT=0`.

- [ ] **Step 1: Add failing normal-exit and directory-aware tests**

Extend `tests/test-timeout.sh` with a child that outlives its parent and a directory
probe:

```bash
cat > "$TMP/linger.sh" <<'LINGER'
#!/usr/bin/env bash
sleep 300 &
echo $! > "$1"
exit 0
LINGER
chmod +x "$TMP/linger.sh"

run_with_timeout 10 "$TMP/linger.sh" "$TMP/linger.pid"
check "normal exit with a live descendant is blocked" "125" "$?"
check "lingering descendant is recorded" "1" "$RUN_GROUP_LINGERED"
check "lingering group was stopped" "1" "$RUN_GROUP_STOPPED"

mkdir "$TMP/run-here"
run_with_timeout_in 10 "$TMP/run-here" bash -c 'pwd > observed-pwd'
check "directory-aware run uses requested cwd" "$TMP/run-here" \
  "$(cat "$TMP/run-here/observed-pwd")"
```

Add an injectable test hook around the final group-presence check so the test can
force the uncertain outcome without leaving a real process behind:

```bash
TIMEOUT_TEST_FORCE_UNCONTAINED=1 run_with_timeout 10 true
check "unconfirmed process-group shutdown exits 126" "126" "$?"
check "unconfirmed process group is not stopped" "0" "$RUN_GROUP_STOPPED"
```

- [ ] **Step 2: Run the timeout test and confirm the new assertions fail**

Run: `bash tests/test-timeout.sh`

Expected: failures for missing `run_with_timeout_in`, missing lingering detection,
and missing exit 126 behavior. Existing timeout assertions remain green.

- [ ] **Step 3: Extend the timeout helper**

Keep `run_with_timeout` backward compatible and route both entry points through one
internal implementation. Start the command with `exec` in its own process group,
retain the group id, and check the negative group id after the direct child exits.
Use these constants and public state:

```bash
TIMEOUT_EXIT=124
LINGERING_EXIT=125
UNCONTAINED_EXIT=126

TIMEOUT_HIT=0
RUN_GROUP_STOPPED=0
RUN_GROUP_LINGERED=0
RUN_GROUP_ID=""

run_with_timeout() {
  local seconds="$1"
  shift
  run_with_timeout_in "$seconds" "$PWD" "$@"
}
```

`run_with_timeout_in` must:

1. validate the directory;
2. launch `(cd "$directory" && exec "$@")` as a separate process group;
3. retain the existing watcher, TERM, five-second grace period, and KILL behavior;
4. wait for the direct child and stop the watcher;
5. check whether any member remains with `kill -0 -"$RUN_GROUP_ID"`;
6. contain a lingering group and return 125 after confirmed cleanup;
7. return 126 if the final group check still finds a member;
8. preserve and restore the caller's signal traps;
9. set the public state in the caller shell.

The `TIMEOUT_TEST_FORCE_UNCONTAINED` branch may only override the final observed
containment state; it must not skip real cleanup.

- [ ] **Step 4: Stop wrapping timeout calls in directory-changing subshells**

In each harness, replace this shape:

```bash
( cd "$dir" && run_with_timeout "$seconds" "${cmd[@]}" </dev/null )
```

with:

```bash
run_with_timeout_in "$seconds" "$dir" "${cmd[@]}" \
  >"$outfile" 2>>"$ERR_FILE" </dev/null
rc=$?
```

The call now runs in the harness shell, so the timeout and process-group state remains
available. Map exit 125 and 126 to harness failure. For exit 126, include
`writer process group could not be confirmed stopped` in `ERR_FILE`. Do not report a
plain command exit 124 as a timeout when `TIMEOUT_HIT=0`.

- [ ] **Step 5: Run focused regression tests**

Run:

```bash
bash tests/test-timeout.sh
bash tests/test-code-delegate.sh
bash tests/test-review-delegate.sh
bash tests/test-probe.sh
```

Expected: every script ends with `FAIL=0`. The new lingering test reports exit 125,
and the existing timeout/session tests remain green.

- [ ] **Step 6: Commit**

```bash
git add scripts/lib/timeout.sh scripts/harness/cursor.sh scripts/harness/codex.sh scripts/harness/claude.sh tests/test-timeout.sh tests/test-code-delegate.sh tests/test-review-delegate.sh tests/test-probe.sh
git commit -m "fix: verify delegated process groups stop"
```

---

### Task 2: Add exclusive worktree ownership and recovery state

**Files:**
- Create: `scripts/lib/coder-lifecycle.sh`
- Create: `tests/test-coder-lifecycle.sh`

**Interfaces:**
- Consumes: a canonical linked-worktree path and Git metadata.
- Produces:
  - `lifecycle_open <cwd> <coder> <resume-lifecycle-id>` — atomically acquires the live lock, validates fresh or resumed admission, and sets lifecycle path/state variables.
  - `lifecycle_update <state> <session-id> <process-group-id> <writer-stopped> <diagnostic>` — atomically writes the recovery record with current Git state and changed paths.
  - `lifecycle_finish` — clears the recovery record and releases the live lock after a successful clean result.
  - `lifecycle_block_recoverable <diagnostic>` — preserves the recovery record and releases only the live lock after confirmed writer shutdown.
  - `lifecycle_quarantine <diagnostic>` — preserves both recovery state and the ownership marker.
  - `lifecycle_recover <cwd> <lifecycle-id>` — proves the old owner stopped and the recorded Git identity still matches, then converts quarantine to recoverable state without changing working files.
  - `LIFECYCLE_ID`, `LIFECYCLE_STATE_FILE`, `LIFECYCLE_LOCK_DIR`, and `LIFECYCLE_DIAGNOSTIC` for the calling script.

- [ ] **Step 1: Write the failing lifecycle test**

Create `tests/test-coder-lifecycle.sh` using the repository's `PASS`/`FAIL` test
style. Build a real repository with a linked worktree:

```bash
ROOT=$(mktemp -d)
MAIN="$ROOT/main"
WT="$ROOT/task-work"
git init -q -b feature/safety "$MAIN"
git -C "$MAIN" config user.email test@example.com
git -C "$MAIN" config user.name Test
echo base > "$MAIN/base.txt"
git -C "$MAIN" add base.txt
git -C "$MAIN" commit -q -m base
git -C "$MAIN" worktree add -q -b feature/safety-work "$WT"
```

Cover these cases with explicit assertions:

```bash
lifecycle_open "$WT" c-codex ""
first_id="$LIFECYCLE_ID"
check "first owner acquires the worktree" "0" "$?"

( source "$LIB"; lifecycle_open "$WT" c-cursor "" ) >/dev/null 2>&1
check "second owner is refused" "1" "$?"

lifecycle_update recoverable sess-1 "" 1 "verification failed"
lifecycle_block_recoverable "verification failed"
( source "$LIB"; lifecycle_open "$WT" c-codex wrong-id ) >/dev/null 2>&1
check "wrong lifecycle cannot resume" "1" "$?"
```

Also assert:

- two different linked worktrees acquire independent locks;
- fresh dirty work is refused;
- a matching lifecycle id can resume recorded dirty work;
- a mismatched coder cannot resume the record;
- successful finish removes both lock and recovery record;
- recoverable block removes the live lock but retains recovery state;
- quarantine retains the ownership marker;
- recovery refuses while the recorded PID or process group is alive;
- recovery removes a stale lock only when liveness and Git identity match;
- recovery preserves every tracked, staged, and untracked file;
- recovery has no force option;
- a primary checkout and a `main` or `master` worktree are rejected.

- [ ] **Step 2: Run the lifecycle test and confirm it fails**

Run: `bash tests/test-coder-lifecycle.sh`

Expected: failure because `scripts/lib/coder-lifecycle.sh` does not exist.

- [ ] **Step 3: Implement the lifecycle state library**

Use the linked worktree's absolute private Git directory:

```bash
git_dir=$(git -C "$cwd" rev-parse --absolute-git-dir) || return 2
LIFECYCLE_LOCK_DIR="$git_dir/claude-subagents-coder.lock"
LIFECYCLE_STATE_FILE="$git_dir/claude-subagents-coder-state.json"
LIFECYCLE_RECOVERY_LOCK="$git_dir/claude-subagents-coder-recovery.lock"
```

Acquire ownership with `mkdir "$LIFECYCLE_LOCK_DIR"`. That creation is the atomic
operation; never implement check-then-create. Write JSON state to a temporary file in
the private Git directory and rename it into place so readers never observe a partial
record.

Use this versioned state shape:

```json
{
  "version": 1,
  "lifecycle_id": "1725912345-12345-8675",
  "state": "active",
  "owner_pid": 12345,
  "process_group_id": 12346,
  "started_at": "2026-09-09T20:00:00Z",
  "coder": "c-codex",
  "session_id": "sess-1",
  "worktree": "/absolute/task-work",
  "branch": "feature/safety-work",
  "start_commit": "0123456789abcdef",
  "observed_commit": "0123456789abcdef",
  "writer_stopped": false,
  "files_changed": ["src/example.ts"],
  "diagnostic": ""
}
```

Generate the lifecycle id from UTC epoch seconds, the owner PID, and Bash `$RANDOM`.
Validate a supplied id against `^[0-9]+-[0-9]+-[0-9]+$` before using it. Record
changed paths with `git status --porcelain=v1 -z` and convert the NUL-delimited paths
to a JSON array without losing spaces in filenames.

`lifecycle_open` acquires the live lock first, then checks recovery state and Git
state while competitors are excluded. On any admission failure it removes only the
lock it just acquired and returns 1 with `LIFECYCLE_DIAGNOSTIC` populated.

`lifecycle_recover` acquires the separate recovery lock atomically, checks the
recorded PID and negative process-group id with signal 0, validates canonical
worktree, branch, observed commit, and changed-path snapshot, and removes only a stale
live lock. It never edits worktree files or Git history. Always release the recovery
lock through a trap.

- [ ] **Step 4: Run the focused lifecycle test**

Run: `bash tests/test-coder-lifecycle.sh`

Expected: all ownership, recovery, quarantine, and file-preservation assertions pass
with `FAIL=0`.

- [ ] **Step 5: Run related worktree tests and commit**

Run:

```bash
bash tests/test-coder-lifecycle.sh
bash tests/test-worktree.sh
bash tests/test-worktree-isolation.sh
```

Expected: every script ends with `FAIL=0`.

Commit:

```bash
git add scripts/lib/coder-lifecycle.sh tests/test-coder-lifecycle.sh
git commit -m "feat: add coder worktree ownership state"
```

---

### Task 3: Validate and run separate verification commands

**Files:**
- Modify: `scripts/code-delegate.sh`
- Modify: `tests/test-code-delegate.sh`

**Interfaces:**
- Consumes: one or more `--verify-cmd <shell-command>` arguments.
- Produces:
  - `VERIFY_COMMANDS` — ordered Bash array.
  - `VERIFY_RESULTS` — JSON array containing `command`, `exit_code`, `timed_out`, and `output` for each command that ran.
  - `verification_mode` — `commands` or `none`.
  - `CSC_VERIFY_TIMEOUT` — per-command timeout, default `1800` seconds.
- Preserves: a single existing `--verify-cmd` invocation and the existing coder retry count.

- [ ] **Step 1: Add failing argument-validation tests**

Extend `tests/test-code-delegate.sh`:

```bash
run --coder c-codex --verify-cmd "" --verify-cmd "true" >/dev/null 2>&1
check "empty verification cannot be mixed with commands" "2" "$?"

run --coder c-codex --verify-cmd "server: npm test" >/dev/null 2>&1
check "human label is rejected before coder invocation" "2" "$?"

run --coder c-codex --verify-cmd "if then" >/dev/null 2>&1
check "invalid shell syntax is rejected" "2" "$?"
```

Use `MOCK_LOG` in each case and assert that the file is absent or empty, proving
validation happened before the coder started.

- [ ] **Step 2: Add failing ordered-execution and retry tests**

Pass two commands that append distinct markers to a temporary log and assert order.
Then make the second command fail once and assert the retry reruns both commands:

```bash
verify_log="$TMP/verify-order"
first="echo first >> '$verify_log'"
second="echo second >> '$verify_log'"
out=$(run --coder c-codex --verify-cmd "$first" --verify-cmd "$second" 2>/dev/null)
check "verification commands preserve order" $'first\nsecond' "$(cat "$verify_log")"
check "verification result has two entries" "2" "$(echo "$out" | jq '.verification|length')"
```

For the retry case, use a counter file in the second command. Expected log:

```text
first
second
first
second
```

Also add a verification command that sleeps past `CSC_VERIFY_TIMEOUT=1`. Assert
`BLOCKED`, `timed_out:true`, a preserved session id, and no coder retry after the
verification timeout.

- [ ] **Step 3: Run the focused test and confirm the new assertions fail**

Run: `bash tests/test-code-delegate.sh`

Expected: failures for repeated command storage, structured verification output,
early label rejection, and verification timeout.

- [ ] **Step 4: Implement verification parsing and validation**

Replace the scalar verification variable with an array:

```bash
VERIFY_COMMANDS=()

--verify-cmd)
  [[ $# -ge 2 ]] || { echo "error: --verify-cmd requires a value" >&2; exit 2; }
  VERIFY_COMMANDS+=("$2")
  shift 2
  ;;
```

Require at least one entry. Permit explicit no-verification only when the array has
one empty entry. For every non-empty command:

```bash
bash -n -c "$verify_command" >/dev/null 2>&1 || input_error
[[ "$verify_command" =~ ^[[:space:]]*[[:alnum:]_.-]+:[[:space:]] ]] && input_error
```

Validation occurs before pool loading, lifecycle acquisition, or coder invocation.
The diagnostic for a label says to remove `server:`/`client:` text and pass each
command with its own `--verify-cmd`.

- [ ] **Step 5: Implement ordered bounded verification**

For each command, call:

```bash
run_with_timeout_in "${CSC_VERIFY_TIMEOUT:-1800}" "$CWD" \
  bash -c "$verify_command"
```

Append one JSON object to `VERIFY_RESULTS` after each command. Stop a pass at the first
failure. A normal nonzero exit feeds its output to the same-session fix prompt. A
timeout or uncontained process group blocks immediately without asking the coder to
continue. After an ordinary fix attempt, clear `VERIFY_RESULTS` and rerun the full
array from index zero.

Retain `verify_output` temporarily as the last failing command's output for backward
compatibility, while adding the structured `verification` array and
`verification_mode` to every result.

- [ ] **Step 6: Run focused and harness regression tests**

Run:

```bash
bash tests/test-code-delegate.sh
bash tests/test-timeout.sh
bash tests/test-probe.sh
```

Expected: every script ends with `FAIL=0`.

- [ ] **Step 7: Commit**

```bash
git add scripts/code-delegate.sh tests/test-code-delegate.sh
git commit -m "feat: support structured verification commands"
```

---

### Task 4: Make the coder script own locking, recovery, and commits

**Files:**
- Modify: `scripts/code-delegate.sh`
- Modify: `scripts/lib/coder-lifecycle.sh`
- Create: `scripts/validate-code-result.sh`
- Modify: `tests/test-code-delegate.sh`
- Modify: `tests/test-coder-lifecycle.sh`
- Create: `tests/test-code-result.sh`
- Modify: `tests/mock-cursor-agent`
- Modify: `tests/mock-codex`
- Modify: `tests/mock-claude`

**Interfaces:**
- Consumes:
  - task mode: `--coder`, `--task-file`, repeated `--verify-cmd`, `--cwd`, `--commit-message`, optional `--max-retries`, `--session`, and `--lifecycle-id`;
  - recovery mode: `recover --cwd <path> --lifecycle-id <id>`.
- Produces the complete JSON contract from the spec: `status`, `coder`, `session_id`, `lifecycle_id`, `attempts`, `verification_mode`, `verification`, `verified`, `changed`, `commit_id`, `files_changed`, `worktree_clean`, `writer_stopped`, `result`, and `diagnostic`.
- Produces `validate-code-result.sh --exit-code <n> --result-file <path>`, which prints the validated one-line JSON unchanged and returns 0 for valid `DONE`, 1 for valid `BLOCKED`, and 2 for malformed or contradictory results.
- Exit contract: `DONE` exits 0, `BLOCKED` exits 1, invalid input exits 2.

- [ ] **Step 1: Upgrade the test fixture to a real linked worktree**

The new lifecycle must reject primary checkouts, so replace the test's standalone
`git init` folder with a main repository plus linked feature worktree. Configure a
test identity and make one base commit before adding the worktree.

Change the `run` helper so every normal task includes a valid commit message:

```bash
run() {
  bash "$SCRIPT" --task-file "$task" --cwd "$WT" \
    --commit-message "test: delegated task" "$@"
}
```

- [ ] **Step 2: Add deterministic mock controls**

Add the same controls to all three fake coder programs:

```bash
if [[ -n "${MOCK_EDIT_FILE:-}" ]]; then
  printf '%s\n' "${MOCK_EDIT_CONTENT:-delegated change}" > "$MOCK_EDIT_FILE"
fi

if [[ -n "${MOCK_LINGER_PID_FILE:-}" ]]; then
  sleep "${MOCK_LINGER_SECONDS:-300}" &
  echo $! > "$MOCK_LINGER_PID_FILE"
fi
```

Place the edit after the session announcement and before the terminal result. Place
the lingering child before the terminal result so the parent can return success while
the descendant remains alive.

- [ ] **Step 3: Add failing commit and result-contract tests**

Add assertions for:

- a mock edit produces `DONE`, `changed:true`, a real `commit_id`, the expected
  `files_changed` entry, `worktree_clean:true`, and `writer_stopped:true`;
- the commit exists on the work branch and the worktree is clean;
- no edit produces `DONE`, `changed:false`, and an empty `commit_id`;
- a missing session id is `BLOCKED` and creates no commit;
- primary checkout, `main`, and `master` are rejected before coder invocation;
- a coder-created commit or branch switch is quarantined and never reset;
- a failed commit or commit-hook-created leftover returns `BLOCKED` and preserves all
  files and any commit already created;
- the script's one stdout line contains every required result field;
- coder answer text claiming success cannot override failed verification.

Create `tests/test-code-result.sh` at this step, before its implementation. Include
one valid changed success, one valid no-change success, one valid blocked result, and
one failing case for every validator invariant listed in Step 9. Also cover invalid
JSON, multiple stdout lines, a missing field, and a field with the wrong JSON type.

Do not trust the mock's answer text. Set `MOCK_RESULT="DONE, everything passed"` while
making verification fail and assert the lifecycle still returns `BLOCKED`.

- [ ] **Step 4: Add failing ownership, timeout, and recovery tests**

Start one lifecycle in the background with `MOCK_SLEEP=30`. Wait until its lock
metadata exists, then invoke a second lifecycle against the same worktree. Assert the
second exits 1, reports `BLOCKED`, does not start another mock process, and leaves the
first task's files unchanged.

Add these cases:

- a normal completed lifecycle releases ownership and allows the next task;
- a timeout after an edit returns the announced session id, retains the edited file,
  creates recovery state, and blocks a fresh task;
- resume with the wrong lifecycle id is refused;
- resume with the matching lifecycle and session ids continues the dirty task;
- a lingering child after a terminal answer is killed, reported as blocked, and its
  PID is no longer live;
- forced uncontained status quarantines the worktree and leaves the lock marker;
- `recover` refuses while the recorded group is live;
- `recover` succeeds after the group is gone, preserves file hashes, and permits only
  the matching resume;
- `recover` rejects unknown options, a wrong lifecycle id, changed branch, changed
  commit, and changed-path drift.

- [ ] **Step 5: Run the focused tests and confirm they fail**

Run:

```bash
bash tests/test-code-delegate.sh
bash tests/test-coder-lifecycle.sh
bash tests/test-code-result.sh
```

Expected: the new ownership, commit, recovery, and result-contract assertions fail;
the result-validator test fails because its script does not exist.

- [ ] **Step 6: Integrate lifecycle ownership before coder execution**

Source `scripts/lib/coder-lifecycle.sh`. Parse and validate the new arguments before
opening the lifecycle. A normal invocation must follow this order:

```text
validate all inputs
acquire worktree ownership
record branch, commit, and initial worktree state
start or resume coder
confirm writer group stopped
run verification and bounded retries
confirm branch and commit were not changed by coder
commit verified changes
confirm final worktree clean
emit result
release ownership while exiting
```

Install signal and exit traps immediately after acquiring the lock. A trap may remove
the live lock only when `RUN_GROUP_STOPPED=1`; otherwise it must persist quarantine.
All failure paths update recovery state before emitting their one JSON result.

The coder prompt must include this exact behavioral boundary:

```text
Edit the files needed for the task. Do not commit, stash, switch branches, reset,
clean, restore files, or run any other Git command that changes repository state.
The caller owns Git state and will commit verified work.
```

- [ ] **Step 7: Move commit ownership into the script**

Record `START_BRANCH` and `START_COMMIT` immediately after admission. After successful
verification, require both still match before staging anything. Use the supplied
single-line commit message, then derive changed paths from the created commit:

```bash
git -C "$CWD" add -A
git -C "$CWD" commit -m "$COMMIT_MESSAGE"
COMMIT_ID=$(git -C "$CWD" rev-parse HEAD)
FILES_CHANGED=$(git -C "$CWD" diff-tree --no-commit-id --name-only -r -z "$COMMIT_ID")
```

Convert the NUL-delimited paths to the JSON array without word splitting. If there
are no changes, skip staging and committing. After a commit, check the worktree again;
hook-created leftovers produce `BLOCKED`, not `DONE`.

Implement `recover` as a separate mode that never loads a coder pool or invokes a
harness. It calls `lifecycle_recover`, emits a structured recovery result, and exits
0 only when the worktree is safe for the matching resume.

- [ ] **Step 8: Build the result from observed state**

Replace positional `emit` arguments with named shell state so every path emits the
same schema. Construct JSON with `jq -n`; pass `verification` and `files_changed` via
`--argjson`. Treat the coder's text only as `result`.

Before emitting `DONE`, enforce these invariants in the script itself:

```bash
[[ -n "$SESSION_ID" ]]
[[ "$RUN_GROUP_STOPPED" -eq 1 ]]
[[ "$WORKTREE_CLEAN" == true ]]
[[ "$VERIFICATION_MODE" == none || "$VERIFIED" == true ]]
[[ "$CHANGED" == false || -n "$COMMIT_ID" ]]
```

Any failed invariant becomes `BLOCKED` with a diagnostic. Do not emit contradictory
success for the controller to repair.

- [ ] **Step 9: Add the executable result validator**

Create `scripts/validate-code-result.sh`. It accepts `--exit-code` and
`--result-file`, requires the file to contain exactly one non-empty line and one JSON
object, validates all required field types, and enforces:

```text
DONE    <=> process exit 0
BLOCKED <=> process exit 1
DONE    => non-empty session_id
DONE    => writer_stopped is true
DONE    => worktree_clean is true
DONE    => verified is true or verification_mode is none
DONE and changed => non-empty commit_id
DONE and not changed => empty commit_id
```

It prints the original JSON line only after validation. Invalid input exits 2 and
prints its diagnostic to stderr. Implement until the test created in Step 3 passes.

- [ ] **Step 10: Run focused regression tests**

Run:

```bash
bash tests/test-code-delegate.sh
bash tests/test-coder-lifecycle.sh
bash tests/test-code-result.sh
bash tests/test-timeout.sh
bash tests/test-worktree.sh
bash tests/test-worktree-isolation.sh
```

Expected: every script ends with `FAIL=0`. Inspect the temporary-process assertions
to confirm no lingering child survived.

- [ ] **Step 11: Commit**

```bash
git add scripts/code-delegate.sh scripts/lib/coder-lifecycle.sh scripts/validate-code-result.sh tests/test-code-delegate.sh tests/test-coder-lifecycle.sh tests/test-code-result.sh tests/mock-cursor-agent tests/mock-codex tests/mock-claude
git commit -m "fix: enforce single-owner coder lifecycle"
```

---

### Task 5: Switch the controller to direct synchronous execution

**Files:**
- Modify: `commands/implement-plans.md`
- Delete: `agents/coder-delegator.md`
- Consume: `scripts/validate-code-result.sh`
- Modify: `tests/test-no-model-names.sh`

**Interfaces:**
- Consumes: the complete result contract produced by `scripts/code-delegate.sh`.
- Produces: the same public `/implement-plans <plan-path>` workflow without an implementation subagent.
- Preserves: coder menu and probe, one worktree per plan, controller-owned task review, same-session fixes, fast-forward after approval, and final branch finishing.

- [ ] **Step 1: Add failing workflow guard assertions**

Extend `tests/test-no-model-names.sh`:

```bash
check "coder-delegator agent removed" "1" \
  "$([[ ! -e "$REPO/agents/coder-delegator.md" ]] && echo 1 || echo 0)"
check "implement-plans directly invokes code-delegate" "1" \
  "$(grep -cE 'scripts/code-delegate\.sh' "$REPO/commands/implement-plans.md")"
check "implement-plans does not dispatch coder-delegator" "0" \
  "$(grep -c 'Dispatch.*coder-delegator' "$REPO/commands/implement-plans.md")"
```

Add a contract checklist assertion that the command requires `session_id`,
`writer_stopped`, `worktree_clean`, and the changed/commit relationship before review.

- [ ] **Step 2: Run the guard test and confirm it fails**

Run: `bash tests/test-no-model-names.sh`

Expected: failures because the coder agent exists and the command still dispatches it.

- [ ] **Step 3: Replace implementation dispatch with synchronous execution**

Rewrite the implementation override in `commands/implement-plans.md` so the
controller:

1. records the task base commit;
2. prepares a task brief and one shell-safe `--verify-cmd` argument per plan command;
3. builds a concise single-line commit message from the task title;
4. calls `scripts/code-delegate.sh` directly with the selected coder and worktree,
   redirecting its one stdout line to a temporary result file;
5. waits for the command to exit and retains its exit status;
6. calls `scripts/validate-code-result.sh` with that exit status and result file;
7. stops on malformed, blocked, recoverable, or quarantined results;
8. reviews only the committed range reported by the lifecycle;
9. sends review fixes directly through the same external coder `session_id`;
10. passes `lifecycle_id` only when resuming a dirty blocked lifecycle;
11. fast-forwards only after review passes, the process stopped, and the worktree is
    clean;
12. confirms no ownership or recovery state remains before starting the next task.

The command must say explicitly that coder answer text and host status are not
completion evidence. It must not call an agent-list or agent-stop operation for the
coder path because no coder subagent exists after this change.

The command captures and validates a lifecycle result using this shell shape, with
its real task values substituted:

```bash
result_file=$(mktemp)
bash "${CLAUDE_PLUGIN_ROOT}/scripts/code-delegate.sh" \
  --coder "$coder_key" \
  --task-file "$task_file" \
  --cwd "$worktree" \
  --commit-message "$commit_message" \
  --verify-cmd "$verify_one" \
  --verify-cmd "$verify_two" >"$result_file"
delegate_rc=$?
bash "${CLAUDE_PLUGIN_ROOT}/scripts/validate-code-result.sh" \
  --exit-code "$delegate_rc" --result-file "$result_file"
```

The command removes its temporary result file after validation. It adds `--session`
for review fixes and adds both `--session` and `--lifecycle-id` when recovering dirty
blocked work.

For a recoverable block, show the lifecycle id, session id, changed paths, and failure
to the user. Resume only the same task. For quarantine, stop and use the explicit
recovery operation; never clear ownership by deleting files or starting another task.

- [ ] **Step 4: Remove the unsafe agent definition**

Delete `agents/coder-delegator.md`. Do not replace it with another shell-capable
implementation agent. `agents/reviewer-delegator.md` remains unchanged.

- [ ] **Step 5: Run workflow guards and delegate regressions**

Run:

```bash
bash tests/test-no-model-names.sh
bash tests/test-code-delegate.sh
bash tests/test-code-result.sh
bash tests/test-review-delegate.sh
```

Expected: every script ends with `FAIL=0`; the guard proves the coder agent is absent
and the direct lifecycle command is present.

- [ ] **Step 6: Commit**

```bash
git add commands/implement-plans.md agents/coder-delegator.md tests/test-no-model-names.sh
git commit -m "fix: run coder tasks synchronously"
```

---

### Task 6: Document and smoke-test the user-visible behavior

**Files:**
- Modify: `README.md`
- Modify: `tests/e2e-smoke.md`

**Interfaces:**
- Consumes: the completed direct lifecycle and recovery command.
- Produces: user-facing operational guidance and real-tool acceptance checks.

- [ ] **Step 1: Update the README behavior description**

Replace the coder-delegator row with a direct `/implement-plans` description. State
these observable guarantees in plain language:

```text
Only one coding task owns the plan worktree at a time. The next task starts only after
the previous coder and its child processes have stopped, verification has completed,
and any changes have been committed. A timeout preserves partial work and stops the
plan. If shutdown cannot be confirmed, the worktree is quarantined until explicit
recovery succeeds.
```

Add `scripts/lib/coder-lifecycle.sh`, `scripts/validate-code-result.sh`,
`tests/test-coder-lifecycle.sh`, and `tests/test-code-result.sh` to the relevant
architecture/test lists. Do not change reviewer documentation or model-pool examples.

- [ ] **Step 2: Update the manual smoke runbook**

Replace the coder-subagent expectation with direct lifecycle behavior. Add concrete
manual checks for:

1. normal task completion returns a session id and commit, leaves the worktree clean,
   and fast-forwards only after review;
2. a second invocation against a deliberately occupied worktree is refused before a
   coder starts;
3. a timed-out fake or safe throwaway task preserves its partial file and blocks the
   next task;
4. the matching lifecycle and session ids resume recoverable work;
5. quarantine recovery refuses while the recorded process is live;
6. two separate verification commands run in order;
7. a command prefixed with `server:` is rejected before coder invocation.

Every smoke step must use a throwaway repository or disposable branch. Cleanup occurs
only after the process is confirmed stopped and the worktree is clean.

- [ ] **Step 3: Search for stale workflow descriptions**

Run:

```bash
rg -n "coder-delegator|dispatch.*coder|DONE.*report|one implementation subagent" README.md commands agents tests/e2e-smoke.md
```

Expected: no supported implementation path refers to `coder-delegator`; historical
specs may retain the old architecture and are not edited.

- [ ] **Step 4: Run the complete automated suite**

Run:

```bash
for test_script in tests/test-*.sh; do
  bash "$test_script" || exit 1
done
```

Expected: every test script ends with `FAIL=0` and the loop exits 0.

- [ ] **Step 5: Check the complete change**

Run:

```bash
git diff --check
git status --short
git log --oneline origin/master..HEAD
```

Expected: no whitespace errors; only files named in this plan are changed; the commit
list contains the completed lifecycle tasks plus the approved design commit.

- [ ] **Step 6: Commit**

```bash
git add README.md tests/e2e-smoke.md
git commit -m "docs: explain safe coder task ownership"
```

## Self-Review

**Spec coverage.** Success criterion 1 maps to Task 5. Criterion 2 maps to Tasks 2
and 4. Criteria 3 and 6 map to Tasks 1, 4, and 5. Criteria 4 and 5 map to Tasks 1,
2, and 4. Criterion 7 maps to Task 4 and its executable result validator. Criterion 8
maps to Task 3. Criterion 9 maps to the regression tests in Tasks 1 through 5 and the
manual smoke coverage in Task 6. Documentation and rollout requirements map to Task
6. There are no uncovered spec requirements.

**Dependency order.** Task 1 establishes observable process-group containment before
the ownership layer depends on it. Task 2 establishes ownership and recovery state.
Task 3 establishes verification input and output. Task 4 combines those pieces and
moves commit authority into the lifecycle. Task 5 changes the controller only after
the direct lifecycle is complete. Task 6 updates user guidance last, when the behavior
is stable.

**Interface consistency.** `lifecycle_id`, `session_id`, `writer_stopped`,
`worktree_clean`, `verification_mode`, `verification`, `files_changed`, and
`commit_id` use the same spelling in producer, validator, controller, and tests.
`run_with_timeout_in`, `TIMEOUT_HIT`, `RUN_GROUP_STOPPED`, `RUN_GROUP_LINGERED`, and
`RUN_GROUP_ID` use the same spelling in the timeout helper and all harness consumers.

**Plan refinements.** The plan fixes the verification timeout at 1800 seconds through
`CSC_VERIFY_TIMEOUT`, assigns contained lingering and uncontained process-group exits
125 and 126, and adds an executable result validator. These choices make requirements
from the spec testable without changing their behavior. No historical specification
is edited during implementation, and the reviewer workflow and model pools remain
unchanged.

## Final Verification

Run the full suite from the repository root:

```bash
for test_script in tests/test-*.sh; do
  bash "$test_script" || exit 1
done
```

Then run:

```bash
git diff --check
git status --short
```

Expected: every test script ends with `FAIL=0`, the test loop exits 0, there are no
whitespace errors, and only the intended branch changes remain.

---

## Execution Handoff

Plan implementation can proceed in either mode:

1. **Subagent-Driven** — dispatch one implementation worker per task and review after
   each task.
2. **Inline Execution** — execute tasks in this session in batches with review
   checkpoints.
