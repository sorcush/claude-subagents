# E2E Smoke Tests (manual — real tools)

Prereqs: install and log in to whichever tools your pool entries use (`cursor-agent`,
`codex`, `claude`). `jq` and bash 5 on PATH. Plugin loaded (`CLAUDE_PLUGIN_ROOT`
set).

## Pool listing

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/pool.sh" list reviewers
bash "$CLAUDE_PLUGIN_ROOT/scripts/pool.sh" list coders
```

Expect: JSON with `role` and `entries` arrays matching
`.claude-plugin/reviewers.json` and `.claude-plugin/coders.json` (keys, labels,
harness names, default flags).

## Probe each pool entry

For every reviewer key in the shipped pool:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/probe.sh" --role reviewer --key <key>
```

Expect: `{"status":"READY",...}` for all four reviewers.

For every coder key:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/probe.sh" --role coder --key <key>
```

Expect: `{"status":"READY",...}` for both coders.

On `FAILED`, check `reason` (`auth`, `trust`, `not-installed`, `bad-model`,
`timeout`, `other`) and fix the environment before continuing.

---

## `/review` end to end

Run once per reviewer key against a real spec in your repo:

```
/review spec <path-to-a-real-spec>
```

For each run:

1. The command shows a menu built from `pool.sh list reviewers`; pick the entry
   under test.
2. Probe passes, then `reviewer-delegator` returns `REVIEWED` with a non-empty
   report and a `session_id`.
3. No files in the repo were modified by the review.
4. The report follows the rubric format (Summary / Strengths / Issues / Verdict).

Repeat with a plan review when you have a plan and its spec:

```
/review plan <plan-path> <spec-path>
```

## Re-review with session (Codex read-only)

Pick a Codex-backed reviewer (`codex-sol` in the shipped pool). Run a spec review,
note the `session_id`, then re-run with the same reviewer and pass that session:

```
/review spec <same-spec-path>
```

On re-review, do **not** show the menu again — resume the prior session. Confirm the
second call also returns `REVIEWED` with the same `session_id`. For Codex entries,
confirm the resumed run stays read-only (no sandbox escape; the harness passes
`-c sandbox_mode="read-only"` on resume).

---

## `/implement-plans` on a throwaway plan

On a feature branch with a clean working tree, create a tiny plan (one task, a simple
verify command) or use an existing small plan.

```
/implement-plans <plan-path>
```

Walk through and confirm:

1. **Worktree created** — `worktree.sh prepare` prints `READY` with a sibling
   `<branch>-work` path; the main checkout is untouched.
2. **Dependency folders cloned** — if the plan needs `node_modules` or similar, check
   `copied` in the prepare output; copies are isolated from the main tree.
3. **Task delegated** — pick a coder from the menu; probe passes; coder-delegator
   returns `DONE` with a real `session_id` and verified result inside the worktree.
4. **Fast-forward** — after your review, `git merge --ff-only <work-branch>` brings
   the commit onto the feature branch.
5. **Worktree removed** — `worktree.sh remove` succeeds when all work was merged.

If `remove` returns `REFUSED`, unmerged commits remain on the work branch — resolve
before deleting.

## Optional failure paths

- **Probe failure** — log out of one tool and confirm the command stops with clear
  advice matching the `reason` field.
- **BLOCKED review** — with auth broken, confirm no fabricated review is returned.
- **Dirty tree** — confirm `/implement-plans` refuses to start with uncommitted
  changes.

## Cleanup

Remove any throwaway branches, worktrees, and temp plans you created for these checks.

---

## Hermes `cursor-reviewer` smoke (controller-run)

Scenario IDs align with `tests/adapter-contract.json`. Do not record results here
until the controller has executed the live workflow.

Prereqs: `hermes`, `cursor-agent`, and Bash 5 on `PATH`; normal user-level
Cursor authentication already completed. Export the two model names to test:
`HERMES_CODER_MODEL` and `HERMES_REVIEWER_MODEL`.

```bash
SMOKE_ROOT="$(mktemp -d)"
export HERMES_HOME="$SMOKE_ROOT/hermes-home"
: "${HERMES_CODER_MODEL:?set the coder model name}"
: "${HERMES_REVIEWER_MODEL:?set the reviewer model name}"
hermes config set plugins.entries.claude-subagents.settings.coder_model \
  "$HERMES_CODER_MODEL"
hermes config set plugins.entries.claude-subagents.settings.reviewer_model \
  "$HERMES_REVIEWER_MODEL"
export REPO_ROOT="/path/to/temporary/repository"
export ADAPTER="$(pwd -P)/hermes/scripts/dispatch.py"
export RUN_ID="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(8))
PY
)"
export DOC_FILE="$REPO_ROOT/docs/spec.md"

repo_fingerprint() {
  python3 - "$REPO_ROOT" <<'PY'
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
digest = hashlib.sha256()
for current, dirs, files in os.walk(root, followlinks=False):
    dirs[:] = sorted(name for name in dirs if name != ".git")
    for name in sorted(dirs + files):
        path = Path(current) / name
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        digest.update(f"{relative}\0{stat.S_IFMT(mode)}\0{stat.S_IMODE(mode)}\0".encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
for command in (
    ("rev-parse", "HEAD"),
    ("for-each-ref", "--format=%(refname) %(objectname)"),
    ("config", "--local", "--list"),
    ("worktree", "list", "--porcelain"),
    ("status", "--porcelain=v1", "--untracked-files=all"),
    ("fsck", "--full", "--no-dangling"),
):
    result = subprocess.run(
        ["git", "-C", str(root), *command],
        check=True,
        capture_output=True,
    )
    digest.update(result.stdout)
common = Path(subprocess.run(
    ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
    check=True, capture_output=True, text=True,
).stdout.strip())
if not common.is_absolute():
    common = (root / common).resolve()
for relative in ("HEAD", "config", "config.worktree", "packed-refs", "hooks", "refs"):
    base = common / relative
    if not base.exists() and not base.is_symlink():
        continue
    paths = [base]
    if base.is_dir():
        paths.extend(sorted(path for path in base.rglob("*")))
    for path in paths:
        rel = path.relative_to(common).as_posix()
        mode = path.lstat().st_mode
        digest.update(f"git:{rel}\0{stat.S_IFMT(mode)}\0{stat.S_IMODE(mode)}\0".encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
effective_hooks = Path(subprocess.run(
    [
        "git", "-C", str(root), "rev-parse",
        "--path-format=absolute", "--git-path", "hooks",
    ],
    check=True, capture_output=True, text=True,
).stdout.strip()).resolve()
digest.update(f"effective-hooks:{effective_hooks}\0".encode())
if effective_hooks.is_dir():
    for path in [effective_hooks, *sorted(effective_hooks.rglob("*"))]:
        rel = path.relative_to(effective_hooks).as_posix()
        mode = path.lstat().st_mode
        digest.update(f"effective-hook:{rel}\0{stat.S_IFMT(mode)}\0{stat.S_IMODE(mode)}\0".encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
worktrees = common / "worktrees"
if worktrees.is_dir():
    for entry in sorted(worktrees.iterdir()):
        for name in ("HEAD", "gitdir", "commondir", "locked", "prunable", "config.worktree"):
            path = entry / name
            if not path.exists() and not path.is_symlink():
                continue
            rel = path.relative_to(common).as_posix()
            mode = path.lstat().st_mode
            digest.update(f"git:{rel}\0{stat.S_IFMT(mode)}\0{stat.S_IMODE(mode)}\0".encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                digest.update(path.read_bytes())
print(digest.hexdigest())
PY
}
```

Initial review and objective no-mutation check:

```bash
BEFORE="$(repo_fingerprint)"
python3 "$ADAPTER" probe --role reviewer --run-id "$RUN_ID"
FIRST="$(python3 "$ADAPTER" review \
  --target spec \
  --doc-file "$DOC_FILE" \
  --lenses backend \
  --run-id "$RUN_ID" \
  --repo "$REPO_ROOT")"
jq -e '
  .status == "REVIEWED" and
  (.report | type == "string" and length > 0) and
  (.session_id | type == "string" and length > 0) and
  .snapshot_path == ""
' <<<"$FIRST"
test "$(repo_fingerprint)" = "$BEFORE"
GENERATION="$(jq -er '.generation' <<<"$FIRST")"
SESSION_ID="$(jq -er '.session_id' <<<"$FIRST")"
python3 - "$HERMES_HOME/claude-subagents/review-artifacts/$RUN_ID" <<'PY'
import sys
from pathlib import Path
assert not list(Path(sys.argv[1]).glob("snapshot-*"))
PY
```

Expect: one JSON object with `status: REVIEWED`, non-empty `report` and
`session_id`, empty `snapshot_path`, and `generation` incremented from the
persisted reviewer state. The successful disposable clone is already removed.

Executable missing-session refusal:

```bash
set +e
MISSING_SESSION="$(python3 "$ADAPTER" review \
  --target spec \
  --doc-file "$DOC_FILE" \
  --lenses backend \
  --run-id "$RUN_ID" \
  --expected-generation "$GENERATION" \
  --repo "$REPO_ROOT")"
MISSING_RC=$?
set -e
test "$MISSING_RC" -eq 2
jq -e --argjson generation "$GENERATION" '
  .status == "BLOCKED" and
  .generation == $generation and
  (.diagnostic | contains("session id"))
' <<<"$MISSING_SESSION"
test "$(repo_fingerprint)" = "$BEFORE"
```

Executable dirty-checkout refusal uses a different run so it cannot be confused
with the existing review lifecycle:

```bash
DIRTY_RUN_ID="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(8))
PY
)"
DIRTY_FILE="$REPO_ROOT/unrelated-dirty-check"
printf 'dirty\n' >"$DIRTY_FILE"
set +e
DIRTY_RESULT="$(python3 "$ADAPTER" review \
  --target spec \
  --doc-file "$DOC_FILE" \
  --lenses backend \
  --run-id "$DIRTY_RUN_ID" \
  --repo "$REPO_ROOT")"
DIRTY_RC=$?
set -e
test "$DIRTY_RC" -eq 2
jq -e '
  .status == "BLOCKED" and
  (.diagnostic | contains("not clean"))
' <<<"$DIRTY_RESULT"
rm -f "$DIRTY_FILE"
test "$(repo_fingerprint)" = "$BEFORE"
```

Resume. The adapter must create a fresh independent clone because the successful
first clone no longer exists:

```bash
SECOND="$(python3 "$ADAPTER" review \
  --target spec \
  --doc-file "$DOC_FILE" \
  --lenses backend \
  --run-id "$RUN_ID" \
  --expected-generation "$GENERATION" \
  --session "$SESSION_ID" \
  --repo "$REPO_ROOT")"
jq -e --arg session "$SESSION_ID" '
  .status == "REVIEWED" and
  .session_id == $session and
  .snapshot_path == ""
' <<<"$SECOND"
test "$(repo_fingerprint)" = "$BEFORE"
GENERATION="$(jq -er '.generation' <<<"$SECOND")"
```

Expect: the same `session_id`, a new `generation`, and another `REVIEWED`
result from a different disposable clone. Close the cycle and prove completion
is idempotent through the retained tombstone:

```bash
COMPLETE="$(python3 "$ADAPTER" state \
  --action complete \
  --run-id "$RUN_ID" \
  --expected-generation "$GENERATION")"
GENERATION="$(jq -er '.generation' <<<"$COMPLETE")"
REPEATED="$(python3 "$ADAPTER" state \
  --action complete \
  --run-id "$RUN_ID" \
  --expected-generation "$GENERATION")"
jq -e --argjson generation "$GENERATION" '
  .state == "complete" and .generation == $generation
' <<<"$REPEATED"
python3 "$ADAPTER" state --action show --run-id "$RUN_ID" |
  jq -e '.record.state == "complete"'
rm -rf "$SMOKE_ROOT"
```

Scenario coverage: `session-resume`, `missing-session-id`, `dirty-controller-checkout`.

---

## Hermes `cursor-coder` smoke (controller-run)

Run only after deterministic approval. This procedure creates a disposable
repository and performs no remote operation.

```bash
SMOKE_ROOT="$(mktemp -d)"
export HERMES_HOME="$SMOKE_ROOT/hermes-home"
export PYTHONDONTWRITEBYTECODE=1
: "${HERMES_CODER_MODEL:?set the coder model name}"
: "${HERMES_REVIEWER_MODEL:?set the reviewer model name}"
hermes config set plugins.entries.claude-subagents.settings.coder_model \
  "$HERMES_CODER_MODEL"
hermes config set plugins.entries.claude-subagents.settings.reviewer_model \
  "$HERMES_REVIEWER_MODEL"
REPO_ROOT="$SMOKE_ROOT/repo"
git init -b feature/smoke "$REPO_ROOT"
git -C "$REPO_ROOT" config user.name "Hermes Smoke"
git -C "$REPO_ROOT" config user.email "hermes-smoke@example.invalid"
printf 'initial\n' > "$REPO_ROOT/result.txt"
git -C "$REPO_ROOT" add result.txt
git -C "$REPO_ROOT" commit -m initial
RUN_ID="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
ADAPTER="$(pwd -P)/hermes/scripts/dispatch.py"
repo_fingerprint() {
  python3 - "$ADAPTER" "$REPO_ROOT" <<'PY'
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

adapter_path = Path(sys.argv[1]).resolve()
repository = Path(sys.argv[2]).resolve()
spec = importlib.util.spec_from_file_location("hermes_smoke_dispatch", adapter_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
payload = {
    "source": module.controller_source_manifest(repository),
    "git": module.git_control_manifest(repository, repository),
}
fsck = subprocess.run(
    ["git", "-C", str(repository), "fsck", "--full", "--no-dangling"],
    check=True,
    capture_output=True,
)
encoded = json.dumps(
    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
).encode() + fsck.stdout + fsck.stderr
print(hashlib.sha256(encoded).hexdigest())
PY
}
PREPARED="$(python3 "$ADAPTER" worktree --action prepare \
  --repo "$REPO_ROOT" --run-id "$RUN_ID")"
WORKTREE="$(jq -er .worktree <<<"$PREPARED")"
GENERATION="$(jq -er .generation <<<"$PREPARED")"
python3 "$ADAPTER" probe --role coder --run-id "$RUN_ID" |
  jq -e '.status == "READY"'
cat > "$SMOKE_ROOT/task.md" <<'EOF'
Replace result.txt with exactly:
implemented
EOF
if EMPTY="$(python3 "$ADAPTER" code --repo "$REPO_ROOT" --cwd "$WORKTREE" \
  --run-id "$RUN_ID" --task-file "$SMOKE_ROOT/task.md" \
  --expected-generation "$GENERATION" --verify-cmd "" --max-retries 3)"; then
  echo "empty verification unexpectedly succeeded" >&2
  exit 1
else
  EMPTY_RC=$?
fi
test "$EMPTY_RC" -eq 2
jq -e '.status == "BLOCKED" and (.diagnostic | contains("verification"))' <<<"$EMPTY"
python3 "$ADAPTER" state --action show --run-id "$RUN_ID" |
  jq -e --argjson generation "$GENERATION" \
    '.record.state == "prepared" and .generation == $generation'
BEFORE="$(repo_fingerprint)"
if FAILED="$(python3 "$ADAPTER" code --repo "$REPO_ROOT" --cwd "$WORKTREE" \
  --run-id "$RUN_ID" --task-file "$SMOKE_ROOT/task.md" \
  --expected-generation "$GENERATION" \
  --verify-cmd "false" --max-retries 0)"; then
  echo "failed verification unexpectedly succeeded" >&2
  exit 1
else
  FAILED_RC=$?
fi
test "$FAILED_RC" -eq 1
jq -e '.status == "BLOCKED" and .verified == false and (.session_id|length>0)' <<<"$FAILED"
test "$(repo_fingerprint)" = "$BEFORE"
GENERATION="$(jq -er .generation <<<"$FAILED")"
SESSION_ID="$(jq -er .session_id <<<"$FAILED")"
FIRST="$(python3 "$ADAPTER" code --repo "$REPO_ROOT" --cwd "$WORKTREE" \
  --run-id "$RUN_ID" --task-file "$SMOKE_ROOT/task.md" \
  --expected-generation "$GENERATION" --session "$SESSION_ID" \
  --verify-cmd "test \"\$(cat result.txt)\" = implemented" --max-retries 0)"
jq -e '.status == "DONE" and .verified == true and (.session_id|length>0)' <<<"$FIRST"
test "$(repo_fingerprint)" = "$BEFORE"
GENERATION="$(jq -er .generation <<<"$FIRST")"
jq -e --arg session "$SESSION_ID" '.session_id == $session' <<<"$FIRST"
(cd "$WORKTREE" && test "$(cat result.txt)" = implemented)
git -C "$WORKTREE" diff --cached --check
git -C "$WORKTREE" commit -m "implement smoke task"
COMMIT="$(git -C "$WORKTREE" rev-parse HEAD)"
REVIEWING="$(python3 "$ADAPTER" state --action record-reviewing \
  --run-id "$RUN_ID" --expected-generation "$GENERATION")"
GENERATION="$(jq -er .generation <<<"$REVIEWING")"
```

At this point the Hermes controller must inspect the commit, rerun verification,
and perform independent specification/code-quality review. If correction is
required, dispatch it with the original session and no internal retries:

```bash
cat > "$SMOKE_ROOT/correction.md" <<'EOF'
Keep result.txt equal to implemented and add correction.txt containing corrected.
EOF
CORRECTION="$(python3 "$ADAPTER" code --repo "$REPO_ROOT" --cwd "$WORKTREE" \
  --run-id "$RUN_ID" --task-file "$SMOKE_ROOT/correction.md" \
  --expected-generation "$GENERATION" --session "$SESSION_ID" \
  --verify-cmd "test \"\$(cat result.txt)\" = implemented && test \"\$(cat correction.txt)\" = corrected" \
  --max-retries 0)"
jq -e --arg session "$SESSION_ID" \
  '.status == "DONE" and .verified == true and .session_id == $session' <<<"$CORRECTION"
GENERATION="$(jq -er .generation <<<"$CORRECTION")"
git -C "$WORKTREE" diff --cached --check
git -C "$WORKTREE" commit -m "correct smoke task"
COMMIT="$(git -C "$WORKTREE" rev-parse HEAD)"
REVIEWING="$(python3 "$ADAPTER" state --action record-reviewing \
  --run-id "$RUN_ID" --expected-generation "$GENERATION")"
GENERATION="$(jq -er .generation <<<"$REVIEWING")"
```

Exercise cleanup refusal in a second disposable repository so the successful
integration path remains resumable:

```bash
CLEANUP_REPO="$SMOKE_ROOT/cleanup-repo"
git init -b feature/cleanup "$CLEANUP_REPO"
git -C "$CLEANUP_REPO" config user.name "Hermes Smoke"
git -C "$CLEANUP_REPO" config user.email "hermes-smoke@example.invalid"
printf 'initial\n' > "$CLEANUP_REPO/file.txt"
git -C "$CLEANUP_REPO" add file.txt
git -C "$CLEANUP_REPO" commit -m initial
CLEANUP_RUN="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
CLEANUP_PREPARED="$(python3 "$ADAPTER" worktree --action prepare \
  --repo "$CLEANUP_REPO" --run-id "$CLEANUP_RUN")"
CLEANUP_WORKTREE="$(jq -er .worktree <<<"$CLEANUP_PREPARED")"
printf 'unintegrated\n' > "$CLEANUP_WORKTREE/unintegrated.txt"
git -C "$CLEANUP_WORKTREE" add unintegrated.txt
git -C "$CLEANUP_WORKTREE" commit -m "unintegrated smoke commit"
if CLEANUP_RESULT="$(python3 "$ADAPTER" worktree --action remove \
  --repo "$CLEANUP_REPO" --run-id "$CLEANUP_RUN" \
  --expected-generation "$(jq -er .generation <<<"$CLEANUP_PREPARED")")"; then
  echo "unintegrated cleanup unexpectedly succeeded" >&2
  exit 1
else
  CLEANUP_RC=$?
fi
test "$CLEANUP_RC" -eq 1
jq -e '.status == "BLOCKED" and (.unmerged|length>0)' <<<"$CLEANUP_RESULT"
test -d "$CLEANUP_WORKTREE"
git -C "$CLEANUP_REPO" show-ref --verify \
  "refs/heads/$(jq -er .work_branch <<<"$CLEANUP_PREPARED")"
```

After the controller accepts the corrected commit, integrate and clean up:

```bash
PENDING="$(python3 "$ADAPTER" state --action record-integration \
  --run-id "$RUN_ID" --expected-generation "$GENERATION" --commit "$COMMIT")"
GENERATION="$(jq -er .generation <<<"$PENDING")"
git -C "$REPO_ROOT" merge --ff-only "$COMMIT"
INTEGRATED="$(python3 "$ADAPTER" state --action show --run-id "$RUN_ID")"
jq -e '.record.state == "integrated"' <<<"$INTEGRATED"
GENERATION="$(jq -er .generation <<<"$INTEGRATED")"
REMOVED="$(python3 "$ADAPTER" worktree --action remove \
  --repo "$REPO_ROOT" --run-id "$RUN_ID" \
  --expected-generation "$GENERATION")"
jq -e '.status == "REMOVED"' <<<"$REMOVED"
test ! -e "$WORKTREE"
rm -rf "$SMOKE_ROOT"
```

Scenario coverage: `empty-verification`, `failed-verification`,
`session-resume`, `unintegrated-cleanup`, and controller-owned integration.
