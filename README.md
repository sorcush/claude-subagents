# claude-subagents

Delegation subagents for the Claude Code / superpowers workflow. Claude stays the
controller — it plans, decides, and reviews — while implementation and independent
design or plan review are handed to workers from configurable pools, each run
through Cursor, Codex, or Claude.

## Model pools

Models are not hardcoded in commands or agents. Two JSON files in `.claude-plugin/`
define the pools:

- **Reviewers** — `.claude-plugin/reviewers.json`
- **Coders** — `.claude-plugin/coders.json`

Each entry has this shape:

```json
{
  "key": "cursor-composer",
  "label": "Cursor Composer 2.5",
  "harness": "cursor",
  "model": "composer-2.5",
  "default": true
}
```

- `key` — opaque identifier passed to the delegate scripts.
- `label` — what the user sees in the menu.
- `harness` — which tool runs the model (`cursor`, `codex`, or `claude`).
- `model` — the model id that harness understands.
- `default` — optional; marks the recommended entry in the menu.

See the shipped pool files for the current entries. Run `make models` to print both
pools.

## Subagents and commands

| Subagent | Command | Role |
|---|---|---|
| `reviewer-delegator` | `/review <spec\|plan> <doc-path> [spec-path]` | Runs a read-only review and relays the report verbatim. Cannot author or judge. |
| `coder-delegator` | `/implement-plans <plan-path>` | Shells to a coder in an isolated worktree, runs verify, loops, commits, reports. No code-editing tools. |

Both commands read the pool at run time and **ask which worker to use** before
dispatching. There are no tool-specific command aliases.

For reviews, the controller triages findings via `superpowers:receiving-code-review`.
For implementation, the controller follows `superpowers:subagent-driven-development`
with the coder as the implementer.

## Worktree isolation

When you run `/implement-plans`, the coder works in a sibling git worktree named
`<feature-branch>-work`, not in your main checkout. After each task passes review,
reviewed commits are fast-forwarded onto the feature branch.

Dependency folders listed in the worktree script are brought across as isolated
copies — copy-on-write clones where the filesystem supports them, ordinary copies
otherwise. Any symlink that would resolve outside the worktree is removed. The main
checkout is never written to during delegation.

## Usage

**Review a spec or plan** — pick a reviewer when prompted:

```
/review spec docs/superpowers/specs/2026-01-01-foo-design.md
/review plan docs/superpowers/plans/2026-01-01-foo.md docs/superpowers/specs/2026-01-01-foo-design.md
```

At the brainstorming Spec self-review gate and the writing-plans Self-Review gate,
run `/review` and let the user choose the reviewer. For spec reviews, lenses
(backend always; frontend/ui when the spec has a UI surface) are selected
automatically. On re-review, pass the prior `session_id` so the reviewer resumes
its context.

**Implement a plan** — pick a coder when prompted:

```
/implement-plans docs/superpowers/plans/2026-01-01-foo.md
```

Requires a feature branch and a clean working tree. The command creates the
worktree, delegates each task, fast-forwards after each passing review, and removes
the worktree when done.

Review is code- and document-based, not visual: external tools cannot render or
screenshot a UI.

## Adding a model

Add one entry to `.claude-plugin/reviewers.json` or `.claude-plugin/coders.json`.
No regeneration step — the orchestrator reads the pool at run time.

## Adding a tool

1. Add one file in `scripts/harness/` defining `harness_probe`, `harness_run`, and
   `harness_render`.
2. Add a `tests/mock-<tool>` for unit tests.
3. Add pool entries that reference the new harness name.

## Requirements

- `cursor-agent`, `codex`, and `claude` on `PATH` and logged in for whichever pool
  entries you intend to use.
- `jq` and bash 5.x on `PATH`.
- The **superpowers** plugin — both commands plug into its skills
  (`subagent-driven-development`, `requesting-code-review`, `receiving-code-review`,
  and the brainstorming / writing-plans gates).

## Installing

This repo hosts a Claude Code marketplace named **`qc-point`**. Install is two steps —
register the marketplace by pointing at the repo that contains it, then install the
plugin from that marketplace:

```
# 1. Add the marketplace. The argument is the GitHub repo that HOSTS the marketplace
#    (owner/repo) — NOT a marketplace or plugin name. Claude Code reads
#    .claude-plugin/marketplace.json and registers it as "qc-point".
/plugin marketplace add sorcush/claude-subagents

# 2. Install the plugin. The form is <plugin-name>@<marketplace-name>.
/plugin install claude-subagents@qc-point
```

## Updating an installed plugin

Git-backed marketplaces do **not** auto-refresh by default:

```
/plugin marketplace update qc-point     # refresh the cached marketplace
```

Claude Code then detects the newer `version` from `plugin.json` and updates the
installed plugin (you may be prompted to `/reload-plugins` or restart). To update
automatically on startup, enable auto-update for the `qc-point` marketplace in the
`/plugin` UI → **Marketplaces** tab.

## Releasing a new version (maintainers)

Version lives in `.claude-plugin/plugin.json` and follows semver. Because the
`version` field is what installed users pin to, **every release must bump it**.
The `Makefile` automates the flow:

```
make bump-patch    # 1.0.0 -> 1.0.1   (backward-compatible fixes)
make bump-minor    # 1.0.1 -> 1.1.0   (new features, backward-compatible)
make bump-major    # 1.1.0 -> 2.0.0   (breaking changes)
make release       # commit the bump as "release: vX.Y.Z" and push to origin
make version       # print the current version
make models        # print the reviewer and coder pools
```

`make release` refuses to run on a clean working tree, so run a bump target first.
It also runs the full test suite before committing.

## Tests

```
bash tests/test-pool.sh              # pool loading and listing
bash tests/test-probe.sh             # probe failure classification
bash tests/test-review-delegate.sh   # reviewer delegate unit tests (mock harnesses)
bash tests/test-code-delegate.sh     # coder delegate unit tests (mock harnesses)
bash tests/test-worktree.sh          # worktree prepare/remove lifecycle
bash tests/test-worktree-isolation.sh # main checkout isolation guarantee
bash tests/test-timeout.sh           # bounded timeouts on external calls
bash tests/test-no-model-names.sh    # orchestrator never hardcodes model names
bash tests/test-gen-changelog.sh     # changelog section generation from commit history
bash tests/test-update-changelog.sh  # idempotent changelog file writes
```

See `tests/e2e-smoke.md` for manual end-to-end checks against real tools.

## Hermes Agent

This repository is also a native Hermes plugin. It registers
`claude-subagents:cursor-coder` and `claude-subagents:cursor-reviewer`; it
registers no model-facing tools, hooks, providers, or services. Hermes reads
role models from the active profile and never falls back to `auto`.

Install a released plugin disabled, configure the named profile, then enable it:

```bash
hermes -p super-dev-codex plugins install sorcush/claude-subagents --no-enable
hermes -p super-dev-codex config set \
  plugins.entries.claude-subagents.settings.coder_model composer-2.5
hermes -p super-dev-codex config set \
  plugins.entries.claude-subagents.settings.reviewer_model gpt-5.6-sol-high
hermes -p super-dev-codex plugins enable claude-subagents
```

Start a fresh Hermes session after enablement.

Hermes keeps native plugin skills in a separate qualified registry. They are
always loadable as `claude-subagents:cursor-coder` and
`claude-subagents:cursor-reviewer`, but qualified names do not appear in
`hermes skills list` or the slash palette by default. To expose the bare names
`cursor-coder` and `cursor-reviewer` there too, append this directory to the
profile's existing `skills.external_dirs` list without removing its current
entries:

```text
~/.hermes/profiles/<profile>/plugins/claude-subagents/hermes/skills
```

For unreleased development only, link this checkout at
`$HOME/.hermes/profiles/super-dev-codex/plugins/claude-subagents` instead of
using the published install command; never replace an existing path blindly.
Hermes resolves an existing plugin path before install as a traversal safety
check, so a development symlink whose target is outside the profile will block
a later GitHub install. Unlink that symlink first; `--force` cannot bypass the
path-safety check.

The coder supports one bounded task or a written plan. Cursor edits only a
run-scoped linked worktree, with sandboxing, bounded output, a disposable
verification home, controller verification/review, controller-owned local
commits, and fast-forward-only integration. This authorization never permits
pushes, remote merges, releases, history rewrites, or Git configuration changes.

The reviewer accepts only design specifications and implementation plans. Each
call uses a fresh `--no-local --no-hardlinks` clone and preserves the Cursor
session for re-review. Cursor still runs with `--approve-mcps`; ask mode is not
an operating-system sandbox, so configured MCP side effects remain an accepted
trust boundary.

Recovery records are mode `0600` under
`$HERMES_HOME/claude-subagents/runs/<run-id>.json`; diagnostic review snapshots
are under `$HERMES_HOME/claude-subagents/review-artifacts/<run-id>/`. Do not
remove a coder worktree manually until all commits are integrated. Use the
recorded run ID, generation, and session ID to resume.
Worktree preparation records deterministic branch and path identity in
`preparing` before creation, so rerunning the same prepare command can reconcile
a crash without inventing replacement recovery paths.
Coder records also retain the current task's original protected-state
fingerprint and durable budgets (nine Cursor calls, five controller review
rounds, and five active worker hours). Those fields and the session reset after
integration when the next serial task starts. A protected-state mismatch remains
blocked for diagnosis.

Hermes dispatch requires Python 3, Git, `jq`, `cursor-agent`, and Bash 5 or
newer. Run `python3 hermes/scripts/dispatch.py runtime --bash-path` to see the
selected interpreter and `make test` for all shell and Python contracts.
