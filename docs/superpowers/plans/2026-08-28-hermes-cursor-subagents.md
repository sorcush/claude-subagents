# Hermes Cursor Subagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add profile-configured `cursor-coder` and `cursor-reviewer` Hermes workflows to the existing Claude Code plugin without changing the default Claude execution path.

**Architecture:** A native Hermes plugin registers two namespaced workflow skills. A Python standard-library adapter resolves active-profile settings, manages durable run state, creates isolated coder worktrees or independent reviewer clones, and invokes the existing Bash delegate scripts through opt-in compatibility controls. Existing Claude behavior remains the default whenever the new environment variables are unset.

**Tech Stack:** Bash 5, Python 3 standard library, Git, jq, Cursor Agent CLI, Hermes native plugin API, Superpowers skills.

**Spec:** `docs/superpowers/specs/2026-08-28-hermes-cursor-subagents-design.md`

## Global Constraints

- Target Hermes Agent v0.20.6; older versions have no compatibility claim until tested.
- Declare the `superpowers` plugin dependency in `plugin.yaml` as `>=6.3.0,<7.0.0`; Hermes treats the manifest entry as advisory, and local doctor validates plugins in isolation.
- Register no model-facing Hermes tools, hooks, providers, or background services.
- Do not route Cursor through Hermes `delegate_task` or configure Cursor as an inference provider.
- Store models under `plugins.entries.claude-subagents.settings.coder_model` and `.reviewer_model` with no fallback.
- Require Bash 5 or newer; reject relative PATH entries and repository-local Bash candidates.
- Keep every coder run in a run-scoped linked worktree and every reviewer run in an independent `--no-local --no-hardlinks` clone.
- Require a non-empty verification command and `verified:true` before any coder commit or integration.
- Permit local worktree commits and reviewed fast-forward integration; do not push, publish, remotely merge, or rewrite history.
- Keep reviewed commits when a later plan task fails; preserve failed worktrees and recovery state.
- Keep Cursor reviewer flags `--force --trust --approve-mcps --mode ask` unchanged by explicit user decision; document the MCP side-effect trust boundary.
- Keep edit-capable workers serial. Do not parallelize writers.
- Preserve existing Claude behavior when `CSC_RUN_ID`, `CSC_STREAM_MAX_BYTES`, `CSC_CURSOR_SANDBOX`, and `CSC_VERIFY_HOME` are unset.
- Treat adapter parity as an explicit maintenance contract, not byte-for-byte equivalence.
- Modify only the `super-dev-codex` profile during installation and live verification.
- Do not commit, push, publish, or release during execution unless Andrey explicitly requests it.

---

## File Structure

### New files

- `plugin.yaml` — Hermes native manifest, dependency declaration, and model configuration schema.
- `__init__.py` — fail-closed registration of the two Hermes skills.
- `hermes/scripts/dispatch.py` — profile-aware CLI adapter, run state, subprocess execution, Git guards, and reviewer-clone lifecycle.
- `hermes/skills/cursor-coder/SKILL.md` — ad-hoc and plan-mode controller workflow.
- `hermes/skills/cursor-reviewer/SKILL.md` — specification and plan review workflow.
- `AGENTS.md` — automatically loaded adapter-parity instruction for Hermes-compatible agents.
- `CLAUDE.md` — automatically loaded adapter-parity instruction for Claude Code.
- `docs/adapter-parity.md` — authoritative host-adapter mapping and divergence register.
- `tests/adapter-contract.json` — named shared behavior scenarios.
- `tests/test_hermes_plugin.py` — manifest and registration tests using Python `unittest`.
- `tests/test_hermes_dispatch.py` — wrapper, state, clone, Git guard, and configuration tests.
- `tests/test-adapter-parity.sh` — parity mapping and marker checks.
- `tests/test-cursor-hermes-options.sh` — opt-in Cursor sandbox and stream-limit tests.
- `tests/test-version-sync.sh` — manifest-version equality and release-update checks.

### Modified files

- `scripts/worktree.sh` — optional `CSC_RUN_ID` naming, with unchanged legacy behavior when unset.
- `scripts/harness/cursor.sh` — optional `CSC_STREAM_MAX_BYTES` and `CSC_CURSOR_SANDBOX` behavior.
- `scripts/code-delegate.sh` — optional verification-only `CSC_VERIFY_HOME` handling.
- `tests/mock-cursor-agent` — controlled large-stream and descendant-process fixtures.
- `tests/test-worktree.sh` — run-scoped naming and compatibility cases.
- `tests/test-code-delegate.sh` — disposable verification-home cases.
- `tests/test-timeout.sh` — stream-limit process-group cleanup cases where shared helpers are used.
- `tests/e2e-smoke.md` — matching Claude and Hermes scenario IDs and live workflows.
- `commands/implement-plans.md` — parity marker only.
- `commands/review.md` — parity marker only.
- `agents/coder-delegator.md` — parity marker only.
- `agents/reviewer-delegator.md` — parity marker only.
- `Makefile` — synchronized manifest versions and complete test entry points.
- `README.md` — Hermes installation, configuration, invocation, recovery, and trust-boundary documentation.
- `CHANGELOG.md` — Hermes support entry when a release is prepared.
- `.claude-plugin/plugin.json` — version only when a release is prepared.

---

### Task 1: Add opt-in run-scoped worktrees

**Files:**
- Modify: `scripts/worktree.sh:17-23`
- Modify: `tests/test-worktree.sh`

**Interfaces:**
- Consumes: optional environment variable `CSC_RUN_ID`.
- Produces: legacy names when unset; run branch `<slug>-hermes-<run_id>-work` and sibling directory `<repo>-<slug>-hermes-<run_id>-work` when set.

- [ ] **Step 1: Add failing run-ID validation and naming tests**

Append cases that run `prepare` with these values:

```bash
CSC_RUN_ID=0123456789abcdef
CSC_RUN_ID=bad
CSC_RUN_ID=0123456789abcdeg
```

Assert that the valid value creates:

```text
feature-login-hermes-0123456789abcdef-work
r-scoped-feature-login-hermes-0123456789abcdef-work
```

Assert that invalid values exit non-zero before creating a branch or directory. Create two run IDs against the same feature branch and assert distinct branches and worktrees. Retain the existing exact legacy-name assertions with `CSC_RUN_ID` unset.

- [ ] **Step 2: Run the focused test and observe failure**

Run:

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-worktree.sh
```

Expected: new run-scoped assertions fail while existing legacy assertions pass.

- [ ] **Step 3: Implement deterministic run-scoped naming**

Add the following logic before `WORK` and `WT` are assigned:

```bash
RUN_ID="${CSC_RUN_ID:-}"
if [[ -n "$RUN_ID" && ! "$RUN_ID" =~ ^[a-f0-9]{16}$ ]]; then
  die "CSC_RUN_ID must match ^[a-f0-9]{16}$"
fi

sanitize_slug() {
  local value="$1"
  value="$(printf '%s' "$value" | sed -E 's/[^A-Za-z0-9._-]+/-/g; s/-+/-/g; s/^[._-]+//; s/[._-]+$//')"
  [[ -n "$value" ]] || value=branch
  printf '%.80s' "$value"
}

SLUG="$(sanitize_slug "$FEATURE")"
if [[ -n "$RUN_ID" ]]; then
  WORK="${SLUG}-hermes-${RUN_ID}-work"
  WT="$(dirname "$ROOT")/$(basename "$ROOT")-${SLUG}-hermes-${RUN_ID}-work"
else
  WORK="${FEATURE}-work"
  SLUG="${FEATURE//\//-}"
  WT="$(dirname "$ROOT")/$(basename "$ROOT")-${SLUG}-work"
fi
```

Keep all existing removal, dependency-copy, and loss-prevention behavior
unchanged. Preserve clean reuse for the same original feature branch and run
ID. Persist the original feature identity in the worktree's private Git
directory and reject reuse when a different feature branch sanitizes to the
same slug. Apply the same identity check before run-scoped removal so a
colliding feature cannot remove another feature's worktree.

- [ ] **Step 4: Test valid, invalid, concurrent, prepare/remove, and legacy paths**

Run:

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-worktree.sh
```

Expected: every assertion passes, including exact existing legacy names.

- [ ] **Step 5: Review the diff for accidental default-path changes**

Run:

```bash
git diff -- scripts/worktree.sh tests/test-worktree.sh
```

Expected: all new behavior is guarded by non-empty `CSC_RUN_ID`.

- [ ] **Step 6: Commit if commit authorization is active for the implementation session**

```bash
git add scripts/worktree.sh tests/test-worktree.sh
git commit -m "feat: add run-scoped worktree names"
```

### Task 2: Add opt-in Cursor sandbox and bounded stream capture

**Files:**
- Create: `scripts/lib/run-captured.py`
- Create: `tests/test-cursor-hermes-options.sh`
- Modify: `scripts/harness/cursor.sh:24-115`
- Modify: `tests/mock-cursor-agent`
- Modify: `tests/test-timeout.sh`

**Interfaces:**
- Consumes: optional `CSC_CURSOR_SANDBOX=enabled` and `CSC_STREAM_MAX_BYTES=<positive integer>`.
- Produces: existing Cursor behavior when both are unset; `--sandbox enabled` for edit mode when requested; exit 125 and process-group termination when captured stdout exceeds the configured byte limit.

- [ ] **Step 1: Write failing option and stream-limit tests**

Add tests that assert:

```text
unset CSC_CURSOR_SANDBOX -> no --sandbox argument
CSC_CURSOR_SANDBOX=enabled + edit -> --sandbox enabled
CSC_CURSOR_SANDBOX=enabled + read-only -> existing reviewer argv unchanged
unset CSC_STREAM_MAX_BYTES -> legacy capture behavior
64 KiB limit + 128 KiB mock stream -> failure and no retained partial stream
stream overflow -> mock child and grandchild are both gone
```

Extend `tests/mock-cursor-agent` with `MOCK_STREAM_BYTES` and `MOCK_GRANDCHILD_PID_FILE`. The mock must emit deterministic chunks and spawn a sleeping descendant only when requested.

- [ ] **Step 2: Run focused tests and observe failure**

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-cursor-hermes-options.sh
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-timeout.sh
```

Expected: new sandbox and stream-limit cases fail; existing timeout cases pass.

- [ ] **Step 3: Implement the bounded capture helper**

Create `scripts/lib/run-captured.py` with this CLI:

```text
run-captured.py --timeout-seconds N --max-stdout-bytes N
                --stdout-file PATH --stderr-file PATH --cwd PATH -- COMMAND...
```

Use only `argparse`, `os`, `selectors`, `signal`, `subprocess`, `time`, and `pathlib`. Start the child with `start_new_session=True`. Stream stdout in chunks to the output file, append stderr to the error file, and terminate the process group with `SIGTERM` followed by `SIGKILL` after five seconds on timeout or overflow. Return 124 for timeout, 125 for output overflow, or the child status.

- [ ] **Step 4: Wire optional behavior into the Cursor harness**

Construct Cursor arguments conditionally:

```bash
local -a cmd=("$CSC_CURSOR_BIN" -p --force --trust --approve-mcps
              --output-format stream-json --model "$model")
[[ "$mode" == "read-only" ]] && cmd+=(--mode ask)
[[ "$mode" == "edit" && "${CSC_CURSOR_SANDBOX:-}" == "enabled" ]] \
  && cmd+=(--sandbox enabled)
```

When `CSC_STREAM_MAX_BYTES` is unset, retain the existing `run_with_timeout` path exactly. When set, validate a positive integer and call `run-captured.py`. Translate exit 125 into `stream exceeded <N> bytes` in `ERR_FILE`.

- [ ] **Step 5: Run focused and existing harness tests**

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-cursor-hermes-options.sh
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-timeout.sh
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-probe.sh
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-review-delegate.sh
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-code-delegate.sh
```

Expected: all assertions pass. Reviewer argument tests still observe `--force --trust --approve-mcps --mode ask`.

- [ ] **Step 6: Commit if authorized**

```bash
git add scripts/lib/run-captured.py scripts/harness/cursor.sh tests/mock-cursor-agent tests/test-cursor-hermes-options.sh tests/test-timeout.sh
git commit -m "feat: add opt-in Cursor execution guards"
```

### Task 3: Isolate verification from the user home

**Files:**
- Modify: `scripts/code-delegate.sh:87-103`
- Modify: `tests/test-code-delegate.sh`

**Interfaces:**
- Consumes: optional `CSC_VERIFY_HOME=<absolute-directory>`.
- Produces: verification under a minimal environment when set; unchanged inherited environment when unset.

- [ ] **Step 1: Add failing environment-isolation tests**

Create a temporary home containing `credential.txt`. Run one verification with `CSC_VERIFY_HOME` unset and one with it set to an empty `0700` directory. Assert:

```text
unset -> HOME remains the caller home
set -> HOME equals CSC_VERIFY_HOME
set -> AZURE_OPENAI_API_KEY is absent
set -> PATH, TMPDIR, LANG, USER, and SHELL retain explicitly supplied values
```

Also retain the existing assertion that verification runs in `--cwd`.

- [ ] **Step 2: Run the focused test and observe failure**

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-code-delegate.sh
```

Expected: new isolation assertions fail.

- [ ] **Step 3: Add optional minimal-environment execution**

Split verification execution into two branches:

```bash
if [[ -n "${CSC_VERIFY_HOME:-}" ]]; then
  [[ "$CSC_VERIFY_HOME" = /* && -d "$CSC_VERIFY_HOME" ]] \
    || { VERIFY_RC=2; VERIFY_OUT="invalid CSC_VERIFY_HOME"; return; }
  ( cd "$CWD" && env -i \
      HOME="$CSC_VERIFY_HOME" PATH="$PATH" TMPDIR="${TMPDIR:-/tmp}" \
      LANG="${LANG:-C}" LC_ALL="${LC_ALL:-}" USER="${USER:-}" SHELL="$BASH" \
      "$BASH" -c "$VERIFY_CMD" ) >"$f" 2>&1
else
  ( cd "$CWD" && eval "$VERIFY_CMD" ) >"$f" 2>&1
fi
```

The Hermes wrapper creates and removes `CSC_VERIFY_HOME`. Claude leaves the variable unset and retains current behavior.

- [ ] **Step 4: Run the focused and full existing shell suite**

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-code-delegate.sh
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash -c 'set -e; for t in tests/test-*.sh; do /opt/homebrew/bin/bash "$t"; done'
```

Expected: all discovered tests pass.

- [ ] **Step 5: Commit if authorized**

```bash
git add scripts/code-delegate.sh tests/test-code-delegate.sh
git commit -m "feat: isolate delegated verification home"
```

### Task 4: Add the native Hermes plugin skeleton and synchronized versions

**Files:**
- Create: `plugin.yaml`
- Create: `__init__.py`
- Create: `tests/test_hermes_plugin.py`
- Create: `tests/test-version-sync.sh`
- Modify: `Makefile`

**Interfaces:**
- Consumes: Hermes `PluginContext.register_skill` and manifest schema v2.
- Produces: qualified skills `claude-subagents:cursor-coder` and `claude-subagents:cursor-reviewer`; required settings `coder_model` and `reviewer_model`.

- [ ] **Step 1: Write failing manifest and registration tests**

Use `unittest` and a fake context:

```python
class FakeContext:
    def __init__(self):
        self.skills = []

    def register_skill(self, name, path, description="", frontmatter=None):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        self.skills.append((name, Path(path), description, dict(frontmatter or {})))
```

Assert that `register(FakeContext())` registers exactly `cursor-coder` and `cursor-reviewer`, both paths end in `SKILL.md`, and both descriptions are non-empty. Assert that registration still succeeds when the fake context reports no loaded sibling plugins, matching doctor-style isolation. Assert that `plugin.yaml` declares the advisory `superpowers` dependency. Add malformed-frontmatter and missing-file tests that expect registration failure.

In `tests/test-version-sync.sh`, assert that `jq -r .version .claude-plugin/plugin.json` equals the YAML manifest version read through a short Python `yaml.safe_load` command.

- [ ] **Step 2: Run tests and observe failure**

```bash
python3 -m unittest tests/test_hermes_plugin.py -v
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-version-sync.sh
```

Expected: both fail because the Hermes plugin files do not exist.

- [ ] **Step 3: Create the manifest**

Create `plugin.yaml` with these fields:

```yaml
name: claude-subagents
version: 2.2.0
manifest_version: 1
api_version: 1
description: Cursor coder and document-reviewer workflows for Hermes Agent
author: Andrey Sloutsman
license: MIT
platforms: [macos, linux]
requires_plugins:
  - id: superpowers
    version_range: ">=6.3.0,<7.0.0"
config_schema:
  coder_model:
    type: str
    description: Cursor model used by cursor-coder
    required: true
  reviewer_model:
    type: str
    description: Cursor model used by cursor-reviewer
    required: true
```

- [ ] **Step 4: Implement fail-closed skill registration**

In root `__init__.py`, use `pathlib.Path` and `yaml.safe_load`. Implement:

```python
def _load_skill(path: Path) -> tuple[str, str, dict]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"missing frontmatter: {path}")
    _, frontmatter_text, _ = raw.split("---", 2)
    frontmatter = yaml.safe_load(frontmatter_text)
    name = str(frontmatter["name"])
    description = str(frontmatter["description"])
    return name, description, frontmatter


def register(ctx) -> None:
    root = Path(__file__).resolve().parent
    for bare in ("cursor-coder", "cursor-reviewer"):
        path = root / "hermes" / "skills" / bare / "SKILL.md"
        name, description, frontmatter = _load_skill(path)
        if name != bare:
            raise ValueError(f"skill name mismatch: expected {bare}, got {name}")
        ctx.register_skill(name, path, description, frontmatter)
```

Create minimal valid skill files containing only frontmatter and a heading so registration tests can pass; workflow bodies arrive in Tasks 7 and 8.

- [ ] **Step 5: Update release tooling without releasing**

Add `HERMES_PLUGIN_YAML := plugin.yaml`. Make `_bump` write both candidate files to temporary paths, validate JSON and YAML, verify equal new versions, then replace both originals. Add a `check-version-sync` target and run it before release tests. Do not bump version in this task.

- [ ] **Step 6: Run plugin and version tests**

```bash
python3 -m unittest tests/test_hermes_plugin.py -v
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-version-sync.sh
hermes -p super-dev-codex plugins doctor . --ci
```

Expected: all commands exit 0 and plugin doctor reports no validation errors.

- [ ] **Step 7: Commit if authorized**

```bash
git add plugin.yaml __init__.py hermes/skills tests/test_hermes_plugin.py tests/test-version-sync.sh Makefile
git commit -m "feat: add Hermes plugin skeleton"
```

### Task 5: Implement configuration, Bash discovery, pools, and probes

**Files:**
- Create: `hermes/scripts/dispatch.py`
- Create: `tests/test_hermes_dispatch.py`

**Interfaces:**
- Produces CLI operations `probe`, `worktree`, `state`, `code`, and `review`.
- Reads models only through `hermes config get plugins.entries.claude-subagents.settings.<role>_model`.
- Produces one JSON object on stdout; diagnostics go to stderr.

- [ ] **Step 1: Write failing unit tests for pure helpers**

Define tests for these signatures:

```python
def find_bash5(repo: Path, path_value: str) -> Path
def read_model(role: str, hermes_bin: str, env: dict[str, str]) -> str
def write_pool(role: str, model: str, directory: Path) -> Path
def validate_run_id(value: str) -> str
def add_envelope_fields(payload: dict, run_id: str, generation: int | None) -> dict
```

Cover `/bin/bash` rejection on macOS, Homebrew Bash selection, repository-local candidate rejection, missing `HERMES_HOME`, missing config, blank config, invalid role, invalid internal sandbox mode, model characters rejected by the existing pool schema, pool mode `0600`, and exact coder/reviewer pool keys.

- [ ] **Step 2: Run unit tests and observe failure**

```bash
python3 -m unittest tests/test_hermes_dispatch.py -v
```

Expected: import failure for `hermes.scripts.dispatch`.

- [ ] **Step 3: Implement the parser and pure helpers**

Use `argparse` subparsers, including `runtime --bash-path` for build tooling. The runtime operation performs only Bash discovery and does not require `HERMES_HOME` or plugin configuration. Resolve the repository root from `Path(__file__).resolve().parents[2]`. Use `subprocess.run([hermes_bin, "config", "get", key], text=True, capture_output=True)` with inherited `HERMES_HOME`. Create pools with `tempfile.NamedTemporaryFile`, `os.fchmod(fd, 0o600)`, and `json.dump`:

```python
role_key = "cursor-coder" if role == "coder" else "cursor-reviewer"
top_key = "coders" if role == "coder" else "reviewers"
payload = {
    top_key: [{
        "key": role_key,
        "label": "Cursor Coder" if role == "coder" else "Cursor Reviewer",
        "harness": "cursor",
        "model": model,
        "default": True,
    }]
}
```

- [ ] **Step 4: Implement probe routing**

Invoke the existing `scripts/probe.sh` with the selected Bash 5, temporary pool environment, role key, `CSC_STREAM_MAX_BYTES=67108864`, and the supplied run ID. Parse one JSON object, add `run_id`, enforce the 2 MiB final-result cap, and preserve exit status 0 or 1.

- [ ] **Step 5: Test configuration and probe routing with fake executables**

Create fake `hermes`, Bash, and probe executables in temporary directories. Assert exact argv, environment, JSON, exit codes, cleanup, and no profile-path disclosure.

- [ ] **Step 6: Run tests**

```bash
python3 -m unittest tests/test_hermes_dispatch.py -v
hermes -p super-dev-codex plugins doctor . --ci
```

Expected: all tests pass and plugin doctor reports no errors.

- [ ] **Step 7: Commit if authorized**

```bash
git add hermes/scripts/dispatch.py tests/test_hermes_dispatch.py
git commit -m "feat: add Hermes Cursor dispatch foundation"
```

### Task 6: Implement durable run state and guarded worktree operations

**Files:**
- Modify: `hermes/scripts/dispatch.py`
- Modify: `tests/test_hermes_dispatch.py`
- Create: `tests/adapter-contract.json`

**Interfaces:**
- State path: `$HERMES_HOME/claude-subagents/runs/<run_id>.json`.
- Lock path: `$HERMES_HOME/claude-subagents/runs/<run_id>.lock`.
- State mutations use `fcntl.flock`, expected generations, file and directory `fsync`, and atomic replacement.

- [ ] **Step 0: Create the shared scenario fixture**

Create `tests/adapter-contract.json` with this exact structure:

```json
{
  "schema_version": 1,
  "scenarios": [
    {"id": "missing-configuration", "applies_to": ["hermes"]},
    {"id": "authentication-failure", "applies_to": ["claude", "hermes"]},
    {"id": "missing-session-id", "applies_to": ["claude", "hermes"]},
    {"id": "empty-verification", "applies_to": ["hermes"]},
    {"id": "failed-verification", "applies_to": ["claude", "hermes"]},
    {"id": "session-resume", "applies_to": ["claude", "hermes"]},
    {"id": "dirty-controller-checkout", "applies_to": ["claude", "hermes"]},
    {"id": "concurrent-worktrees", "applies_to": ["hermes"]},
    {"id": "unintegrated-cleanup", "applies_to": ["claude", "hermes"]}
  ]
}
```

- [ ] **Step 1: Add failing state-machine tests**

Encode the complete coder and reviewer transition tables as dictionaries and test every allowed and rejected edge. Include:

```python
CODER_TRANSITIONS = {
    "preparing": {"prepared", "blocked"},
    "prepared": {"dispatching", "blocked"},
    "dispatching": {"reviewing", "blocked"},
    "reviewing": {"dispatching", "integration_pending", "blocked"},
    "integration_pending": {"integrated", "blocked"},
    "integrated": {"dispatching", "blocked"},
    "blocked": {"dispatching"},
}
REVIEWER_TRANSITIONS = {
    "dispatching": {"reviewed", "blocked"},
    "reviewed": {"dispatching", "blocked"},
    "blocked": {"dispatching"},
}
```

Test stale generation rejection, booleans rejected as generations, timezone-aware
RFC3339 timestamps, exact common/role/nested key sets, two simultaneous writers,
malformed state, role mismatch, mode `0600`, directory mode `0700`,
parent-directory synchronization, complete tombstone retention, global pruning,
stable lock-file retention, and pruning refusal while a worktree exists.

- [ ] **Step 2: Run state tests and observe failure**

```bash
python3 -m unittest discover -s tests -p 'test_hermes_dispatch.py' -v
```

Expected: state functions are missing.

- [ ] **Step 3: Implement state records and locking**

Implement these functions:

```python
def state_dir(hermes_home: Path) -> Path
def lock_run(hermes_home: Path, run_id: str):
def load_state(hermes_home: Path, run_id: str) -> dict
def create_state(hermes_home: Path, record: dict) -> dict
def transition_state(hermes_home: Path, run_id: str, expected_generation: int,
                     next_state: str, changes: dict) -> dict
def atomic_write_json(path: Path, payload: dict) -> None
def prune_state(hermes_home: Path, now: datetime) -> list[str]
```

`lock_run` is a context manager around `fcntl.flock(fd, LOCK_EX)`. `atomic_write_json` flushes and `os.fsync`s the file, replaces the target with `os.replace`, then opens and `fsync`s the parent directory.
Completed records remain as idempotent tombstones until the 30-day prune.
Per-run lock files are never unlinked. The exact coder `dispatch_baseline` keys
are `pre_dispatch_work_branch_commit`,
`pre_dispatch_feature_branch_commit`, `verified_worker_tree`, and
`verified_worker_index`.

- [ ] **Step 4: Add guarded worktree operations**

`worktree prepare` first persists deterministic recovery identity as generation
1 `preparing`, then sets `CSC_RUN_ID`, invokes existing
`scripts/worktree.sh prepare`, validates its JSON and exact exit status, and
returns generation 2 `prepared`. Retries reconcile no side effect, branch-only,
registered-worktree, and post-shell/pre-state-update crash boundaries under the
same run lock. `worktree remove` holds one run lock across identity/generation
checks, side effects, and complete-tombstone persistence. It invokes removal
with the same run ID, reconciles an absent worktree with a remaining branch and
absence of both, and never fabricates a generation from missing state.

- [ ] **Step 5: Add crash-boundary reconciliation tests**

Cover crashes after worker edits, an unauthorized worker commit, an authorized
controller commit with the exact recorded parent and verified worker tree,
`record-reviewing`, successful and divergent fast-forward before state update,
and partial worktree removal. Each retry must either advance idempotently or
return `BLOCKED` with preserved evidence.

- [ ] **Step 6: Run tests**

```bash
python3 -m unittest discover -s tests -p 'test_hermes_dispatch.py' -v
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-worktree.sh
```

Expected: all tests pass.

- [ ] **Step 7: Commit if authorized**

```bash
git add hermes/scripts/dispatch.py tests/test_hermes_dispatch.py tests/adapter-contract.json
git commit -m "feat: add durable Hermes run state"
```

### Task 7: Implement the Cursor reviewer adapter and skill

**Files:**
- Modify: `hermes/scripts/dispatch.py`
- Modify: `hermes/skills/cursor-reviewer/SKILL.md`
- Modify: `tests/test_hermes_dispatch.py`
- Modify: `tests/e2e-smoke.md`
- Modify: `docs/cursor-reviewer/effectiveness-log.md` during the live smoke workflow

**Interfaces:**
- `dispatch.py review` accepts only `spec` or `plan` and paths inside a Git repository with a valid `HEAD`.
- Each review uses an independent local clone and returns the exact reviewer schema from the specification.

- [ ] **Step 1: Add failing independent-clone tests**

Create a repository with a committed source file and an untracked spec. Assert that review preparation:

```text
rejects non-Git and unborn repositories
rejects symlinked and out-of-repository document paths
rejects unrelated dirty files
creates a clone with a different git-common-dir and object directory
copies the current declared documents into matching relative paths
passes the clone document path to review-delegate.sh
```

Add manifest tests that detect content, executable-bit, symlink-target, path-type, and added-file changes while excluding `.git` metadata.

- [ ] **Step 2: Run reviewer tests and observe failure**

```bash
python3 -m unittest discover -s tests -p 'test_hermes_dispatch.py' -v
```

Expected: review operation is not implemented.

- [ ] **Step 3: Implement independent review clones**

Implement:

```python
def create_review_clone(repository: Path, run_id: str, documents: list[Path]) -> ReviewClone
def source_manifest(root: Path) -> dict[str, dict[str, str | int]]
def run_review(args: argparse.Namespace) -> dict
def cleanup_review_clone(clone: ReviewClone, changed: bool) -> str | None
```

Run:

```text
git clone --no-local --no-hardlinks --no-checkout <repository> <temporary-path>
git -C <temporary-path> checkout --detach <controller-head>
```

Overlay only the declared document and optional specification. Pass the copied
path to `scripts/review-delegate.sh`. Preserve changed or otherwise diagnostic
snapshots below
`$HERMES_HOME/claude-subagents/review-artifacts/<run_id>/`, atomically record
their outcome/path before returning, and remove unchanged snapshots.

- [ ] **Step 4: Implement exact result and state handling**

Start one 1,800-second deadline before repository/document preflight and apply
the remaining budget through clone, checkout, configuration, delegate, parsing,
mutation checks, and cleanup. Run external children in process groups. Reject
raw null/list/numeric report and session values before conversion, empty reports,
and empty session IDs. Initial review rejects session/generation; re-review
requires and exactly matches both plus every persisted identity field, creates a
fresh clone, and transitions `reviewed -> dispatching -> reviewed`. Include
`snapshot_path` only for preserved diagnostic snapshots, preserve legitimate
delegate `BLOCKED` diagnostics after redaction, and return the actual persisted
generation on every lifecycle failure.

- [ ] **Step 5: Write the complete reviewer skill**

The skill must instruct Hermes to:

- accept only specification or plan review;
- derive backend/frontend/UI lenses exactly as the existing command does;
- probe before review;
- preserve and reuse `run_id`, generation, and Cursor session ID;
- relay the report verbatim;
- apply `receiving-code-review` for triage;
- run mutation checks before appending a host-tagged effectiveness entry to `docs/cursor-reviewer/effectiveness-log.md`;
- disclose that `--approve-mcps` is retained and reviewer mode is not an OS sandbox.

Use this concrete structure:

```markdown
---
name: cursor-reviewer
description: Use when reviewing a design spec or implementation plan with Cursor.
version: 1.0.0
metadata:
  hermes:
    tags: [cursor, review, delegation]
    requires_toolsets: [terminal, file]
---

# Cursor Reviewer

Accept only `spec` or `plan` targets. Resolve and preserve one run ID. Execute
`${HERMES_SKILL_DIR}/../../scripts/dispatch.py probe --role reviewer --run-id
<run-id>` before
review. Execute `dispatch.py review` with the document paths, target, lenses,
run ID, latest generation, and prior Cursor session ID on re-review. Treat any
non-REVIEWED result, empty report, empty session ID, or snapshot mutation as
BLOCKED. Relay the report verbatim, then use `receiving-code-review` to verify
and triage findings. Append the host-tagged effectiveness record only after the
worker-mutation check. The retained Cursor MCP approval is an accepted trust
boundary and not an operating-system sandbox.
```

- [ ] **Step 6: Run deterministic reviewer tests**

```bash
python3 -m unittest discover -s tests -p 'test_hermes_dispatch.py' -v
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-review-delegate.sh
```

Expected: all tests pass.

- [ ] **Step 7: Run the real reviewer smoke test**

Use a temporary repository and the configured reviewer model. Verify a non-empty session ID, zero controller-repository mutation before logging, and successful resume after deleting the original clone. Record the exact command and result in `tests/e2e-smoke.md`.

- [ ] **Step 8: Commit if authorized**

```bash
git add hermes/scripts/dispatch.py hermes/skills/cursor-reviewer/SKILL.md tests/test_hermes_dispatch.py tests/e2e-smoke.md
git commit -m "feat: add Hermes Cursor reviewer"
```

### Task 8: Implement guarded Cursor coder dispatch

**Files:**
- Modify: `hermes/scripts/dispatch.py`
- Modify: `hermes/skills/cursor-coder/SKILL.md`
- Modify: `tests/test_hermes_dispatch.py`
- Modify: `tests/e2e-smoke.md`
- Modify: `docs/cursor-coder/effectiveness-log.md` during the live smoke workflow

**Interfaces:**
- `dispatch.py code` requires a non-empty verification command, a valid expected generation, and the exact run-scoped worktree.
- The external Cursor process may edit files but may not commit or alter Git control-plane state.
- The Hermes controller owns commits, review, state transitions, and fast-forward integration.
- State persists the repository object format, original protected-manifest
  digests, Cursor-call count, controller review-round count, and cumulative
  active worker seconds. Initial retries are capped at three; corrections use
  zero retries; task totals are nine calls, five rounds, and five active hours.
- Protected-state checks include Git's effective absolute or relative
  `core.hooksPath`. Blocked resumes compare against the original persisted
  baseline before dispatch.
- Reconciliation recognizes an integrated candidate only when it remains the
  exact authorized work-branch tip. A newer work-branch commit blocks even when
  the feature branch already contains the reviewed candidate.
- `integrated -> dispatching` resets the prior task's session, three budget
  counters, protected manifest, and verified baseline. The next plan task uses
  initial retries and a new session; only same-task corrections use the prior
  session with zero retries.

- [ ] **Step 1: Add failing coder precondition tests**

Assert rejection before Cursor starts for:

```text
controller checkout passed as --cwd
worktree from another repository
unregistered directory
wrong run-scoped branch
missing or empty verification command
stale generation
missing run state
```

- [ ] **Step 2: Add failing postcondition tests**

Extend the mock worker to attempt each prohibited change:

```text
write directly into controller checkout
create a commit on the work branch
move the feature branch ref
change repository config
add or modify a hook
add or remove a worktree registration
remove a reachable object
```

Assert `BLOCKED`, no integration, and preserved evidence. Assert ordinary worktree file edits succeed.

- [ ] **Step 3: Run coder tests and observe failure**

```bash
python3 -m unittest discover -s tests -p 'test_hermes_dispatch.py' -v
```

Expected: coder operation and Git guards are missing.

- [ ] **Step 4: Implement pre/post manifests and coder routing**

Implement:

```python
def git_control_manifest(repository: Path, worktree: Path) -> dict
def controller_source_manifest(repository: Path) -> dict
def validate_coder_workspace(repository: Path, worktree: Path, state: dict) -> None
def run_coder(args: argparse.Namespace) -> dict
def verify_git_postconditions(before: dict, repository: Path, worktree: Path) -> None
```

Run the delegate with:

```text
CSC_CURSOR_SANDBOX=enabled
CSC_STREAM_MAX_BYTES=67108864
CSC_VERIFY_HOME=<empty-0700-temp-dir>
CSC_CODERS_JSON=<mode-0600-temp-pool>
```

After the delegate returns, compare manifests and run `git fsck --full`. Require `DONE`, `verified:true`, a non-empty session ID, and unchanged refs before returning success.

- [ ] **Step 5: Write the complete coder skill**

The skill must define both modes.

For ad-hoc mode, require one bounded task and one verification command, create one run, dispatch, inspect, verify, commit inside the worktree, review, record integration intent, fast-forward, reconcile state, and clean up.

For plan mode, load `subagent-driven-development`, preserve its ledger and five-round review cap, replace only the implementer boundary, use `--max-retries 3` initially and `--max-retries 0` for later review corrections, keep task reviews with Hermes, and retain reviewed partial commits after later failures.

Both modes must retain the same run ID and generation, report recovery state on failure, append the host-tagged outcome to `docs/cursor-coder/effectiveness-log.md`, and never push or rewrite history.

Use this concrete structure:

```markdown
---
name: cursor-coder
description: Use when implementing a bounded task or written plan with Cursor.
version: 1.0.0
metadata:
  hermes:
    tags: [cursor, coding, delegation, worktree]
    requires_toolsets: [terminal, file, todo]
---

# Cursor Coder

For an ad-hoc request, require one bounded task and one non-empty verification
command. For a written plan, load `subagent-driven-development`, preserve its
ledger and review gates, and replace only the implementer boundary. Create one
run ID and one run-scoped worktree. Probe first. Dispatch with the latest state
generation. Require DONE, verified:true, and a Cursor session ID. Independently
inspect and verify the worktree, create the local worktree commit, review it,
record integration intent, fast-forward, reconcile state, and clean up safely.
Use the original Cursor session for correction rounds. Keep already reviewed
commits when a later task blocks. Never push, publish, remotely merge, rewrite
history, or let Cursor approve its own work.
```

- [ ] **Step 6: Add controller-commit and reconciliation tests**

Exercise:

```text
worker edits without committing -> controller commit succeeds
worker commits autonomously -> blocked
review correction resumes original Cursor session
crash after controller commit -> reconcile to reviewing
crash after fast-forward -> reconcile to integrated
later plan task failure -> prior reviewed commits remain
cleanup with unintegrated commits -> refused
```

- [ ] **Step 7: Run deterministic coder tests**

```bash
python3 -m unittest discover -s tests -p 'test_hermes_dispatch.py' -v
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-code-delegate.sh
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-worktree.sh
```

Expected: all tests pass.

- [ ] **Step 8: Run the real coder smoke test**

In a temporary feature branch, dispatch a one-file task with a deterministic test. Verify the controller checkout remains unchanged before integration, the external worker creates no commit, the controller commit is reviewed, fast-forward succeeds, resume uses the original Cursor session, and cleanup removes only integrated work.

- [ ] **Step 9: Commit if authorized**

```bash
git add hermes/scripts/dispatch.py hermes/skills/cursor-coder/SKILL.md tests/test_hermes_dispatch.py tests/e2e-smoke.md
git commit -m "feat: add Hermes Cursor coder"
```

### Task 9: Add adapter-parity guardrails and user documentation

**Files:**
- Create: `AGENTS.md`
- Create: `CLAUDE.md`
- Create: `docs/adapter-parity.md`
- Create: `tests/test-adapter-parity.sh`
- Modify: `commands/implement-plans.md`
- Modify: `commands/review.md`
- Modify: `agents/coder-delegator.md`
- Modify: `agents/reviewer-delegator.md`
- Modify: `README.md`
- Modify: `tests/e2e-smoke.md`

**Interfaces:**
- Every duplicated adapter points to `docs/adapter-parity.md`.
- Shared scenario IDs come from `tests/adapter-contract.json`.

- [ ] **Step 1: Write failing parity tests**

Make `tests/test-adapter-parity.sh` assert that all mapped files exist and contain this exact marker format:

```text
ADAPTER-PARITY: <counterpart-path>; policy: docs/adapter-parity.md
```

Assert that `AGENTS.md` and `CLAUDE.md` both name the parity document and require classification of shared versus host-specific changes.

- [ ] **Step 2: Run the parity test and observe failure**

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-adapter-parity.sh
```

Expected: missing context files, parity document, and markers.

- [ ] **Step 3: Create the parity policy**

`docs/adapter-parity.md` must contain the four mappings from the specification, the shared/Claude/Hermes classification lists, the update procedure, and a divergence table with these initial entries:

```text
Hermes uses profile config; Claude uses pool JSON.
Hermes calls delegates directly; Claude uses restricted delegator agents.
Hermes coder uses run-scoped worktrees; Claude retains legacy deterministic naming.
Hermes coder enables Cursor sandbox and a disposable verification home; Claude retains current defaults.
Hermes reviewer uses an independent clone; Claude reviews the current checkout read-only.
```

- [ ] **Step 4: Create automatically loaded maintenance instructions**

Use this concise content in both root files, with the host name adjusted:

```markdown
# Repository Maintenance

Before changing any file listed in `docs/adapter-parity.md`, read that document.
Classify the change as shared, Claude-specific, or Hermes-specific. Update both
adapters for shared behavior, record intentional divergence, and run both host
contract suites. Do not infer parity from matching filenames or prose.
```

- [ ] **Step 5: Add markers without changing Claude workflow behavior**

Insert HTML comments immediately after frontmatter in the four Claude Markdown files and normal comments near the top of both Hermes skills. Do not alter the existing Claude instructions.

- [ ] **Step 6: Document installation and operation**

Add README sections for:

- dual-host architecture;
- published install-disabled/configure/enable sequence;
- development symlink installation for testing unreleased changes;
- qualified skill names;
- coder task and plan modes;
- reviewer targets;
- worktree and review-clone isolation;
- local-commit authorization and remote-operation prohibition;
- model update commands;
- recovery-state location;
- accepted reviewer MCP trust boundary;
- Bash 5 requirement.

Extend `tests/e2e-smoke.md` so Claude and Hermes checks use the same scenario IDs from `tests/adapter-contract.json`.

- [ ] **Step 7: Run parity and documentation checks**

```bash
PATH=/opt/homebrew/bin:$PATH /opt/homebrew/bin/bash tests/test-adapter-parity.sh
claude plugin validate .
hermes -p super-dev-codex plugins doctor . --ci
```

Expected: all commands exit 0. Claude's component inventory contains only the pre-existing commands and agents.

- [ ] **Step 8: Commit if authorized**

```bash
git add AGENTS.md CLAUDE.md docs/adapter-parity.md commands agents hermes/skills README.md tests/test-adapter-parity.sh tests/e2e-smoke.md
git commit -m "docs: define Claude and Hermes adapter parity"
```

### Task 10: Integrate release gates and verify the complete system

**Files:**
- Modify: `Makefile`
- Modify: `CHANGELOG.md` only when preparing a release
- Modify: `.claude-plugin/plugin.json` and `plugin.yaml` only when preparing a release

**Interfaces:**
- `make test` runs every `tests/test-*.sh` with Bash 5 and every `tests/test_*.py` with Python 3.
- `make check-version-sync` fails before writing when manifest versions differ.

- [ ] **Step 1: Add the complete test target**

Define:

```make
PYTHON ?= python3
BASH5 ?= $(shell $(PYTHON) hermes/scripts/dispatch.py runtime --bash-path 2>/dev/null)

.PHONY: test check-version-sync

test: check-version-sync
	@test -n "$(BASH5)" || { echo "Bash 5 or newer is required" >&2; exit 2; }
	@set -e; for t in tests/test-*.sh; do "$(BASH5)" "$$t"; done
	@$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
```

Make the Bash resolver fail when `$(BASH5)` is older than version 5. Keep release dependent on `test` rather than maintaining a second test loop.

- [ ] **Step 2: Make version bumps synchronized and recoverable**

Generate both updated manifests into temporary files. Validate both before replacing either. Keep backups until both replacements and `check-version-sync` succeed. On any failure, restore both originals and exit non-zero. Add test fixtures that force the second replacement to fail and assert both originals retain their prior versions.

- [ ] **Step 3: Run the full deterministic suite**

```bash
make test
```

Expected: every discovered shell and Python test passes. Report the observed test and assertion counts rather than comparing them with the design-time count of 285.

- [ ] **Step 4: Validate both hosts**

```bash
claude plugin validate .
hermes -p super-dev-codex plugins doctor . --ci
```

Expected: both exit 0. Compare Claude component inventory with the baseline and confirm no Hermes skill is discovered as a Claude component.

- [ ] **Step 5: Link the unreleased plugin into only `super-dev-codex` and configure it**

```bash
PROFILE_HOME="$HOME/.hermes/profiles/super-dev-codex"
PLUGIN_PATH="$PROFILE_HOME/plugins/claude-subagents"
test ! -e "$PLUGIN_PATH"
ln -s "$(pwd -P)" "$PLUGIN_PATH"
hermes -p super-dev-codex config set plugins.entries.claude-subagents.settings.coder_model composer-2.5
hermes -p super-dev-codex config set plugins.entries.claude-subagents.settings.reviewer_model gpt-5.6-sol-high
hermes -p super-dev-codex plugins enable claude-subagents
```

Before and after, record checksums of every other profile's `config.yaml` and plugin enablement state. Expected: only `super-dev-codex` changes. Leave the symlink installed for this profile after successful verification. The README's published-user path uses `hermes plugins install sorcush/claude-subagents --no-enable` only after a release containing the Hermes files exists.

- [ ] **Step 6: Run live reviewer verification**

Start a fresh session. Load `claude-subagents:cursor-reviewer`. Run a specification review and a resumed re-review in a different independent clone. Expected: both return the same non-empty Cursor session ID, the controller repository is unchanged before intentional effectiveness logging, and snapshot cleanup follows the mutation policy.

- [ ] **Step 7: Run live coder verification**

Use a disposable repository and feature branch. Load `claude-subagents:cursor-coder`. Run an ad-hoc task, force one correction, and verify:

```text
real probe returns READY
all edits stay in the run-scoped worktree
Cursor creates no commit
Hermes commits only after verified:true
controller review precedes fast-forward
correction resumes the original Cursor session
cleanup refuses unintegrated commits and removes integrated work
```

- [ ] **Step 8: Verify final repository state**

```bash
git status --short --branch
git diff --check
```

Expected: only intended implementation and documentation files differ. No temporary pools, run-state files, review clones, or worktrees exist inside the repository.

- [ ] **Step 9: Commit only if explicitly requested**

If Andrey requests a commit, create one reviewed implementation commit or retain the staged task commits produced during execution. Do not push or release.
