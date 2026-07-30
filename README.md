# cursor-subagent-cc

Two Cursor-backed delegation subagents for the Claude Code / superpowers workflow,
in one plugin. Claude/Opus stays the controller; Cursor's `cursor-agent` does the
work that benefits from a different model:

- **Implementation** is delegated to Cursor's **<!-- model:coder:label -->Composer 2.5<!-- /model:coder:label -->**.
- **Independent review** of a design spec or plan is delegated to **<!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label -->**
  via `cursor-agent`, or to **<!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label -->** via the Codex CLI (`codex exec`) —
  two independently-sourced reviewers, so the model that authored a doc is never the
  model that grades it, and you choose which one reviews each time.

## Subagents & commands
| Subagent (Haiku) | Command | Delegates to | Role |
|---|---|---|---|
| `cursor-coder-delegator` | `/cursor-implement-plans <plan-path>` | <!-- model:coder:label -->Composer 2.5<!-- /model:coder:label --> | Shells to Composer, runs the verify command, loops, commits, reports. No code-editing tools. |
| `cursor-reviewer-delegator` | `/cursor-review <spec\|plan> <doc-path> [spec-path]` | <!-- model:reviewer:label -->Grok 4.5 (high effort, fast)<!-- /model:reviewer:label --> (read-only) | Runs the review script and relays the report verbatim. Cannot author or judge. |
| `codex-reviewer-delegator` | `/codex-review <spec\|plan> <doc-path> [spec-path]` | <!-- model:codex_reviewer:label -->GPT-5.6 Sol<!-- /model:codex_reviewer:label --> (read-only) | Runs the Codex delegate script and relays the report verbatim. Cannot author or judge. |

Opus is the controller for both: it plans/authors, dispatches the subagent, then
reviews (coder) or triages findings via `superpowers:receiving-code-review` (reviewer).

## Requirements
- `cursor-agent` installed and logged in (`cursor-agent status` / `cursor-agent login`).
- `codex` CLI installed and logged in (`codex login status` / `codex login`).
- `jq` and `bash` 5.x on PATH.
- The **superpowers** plugin installed — both commands plug into its skills
  (`subagent-driven-development`, `requesting-code-review`, `receiving-code-review`,
  and the brainstorming / writing-plans gates).

## Usage
**Implement a plan** (delegates each task to Composer):
- New plan, same session: at the superpowers execution handoff, pick subagent-driven and run `/cursor-implement-plans <plan-path>`.
- Prior plan, new session: `/cursor-implement-plans <plan-path>`.

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

## Installing
This repo hosts a Claude Code marketplace named **`qc-point`**. Install is two steps —
register the marketplace by pointing at the repo that contains it, then install the
plugin from that marketplace:

```
# 1. Add the marketplace. The argument is the GitHub repo that HOSTS the marketplace
#    (owner/repo) — NOT a marketplace or plugin name. Claude Code reads
#    .claude-plugin/marketplace.json and registers it as "qc-point".
/plugin marketplace add sorcush/cursor-subagent-cc

# 2. Install the plugin. The form is <plugin-name>@<marketplace-name>.
/plugin install cursor-subagent-cc@qc-point
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
```
`make release` refuses to run on a clean working tree, so run a bump target first.

## Tests
```
bash tests/test-cc-delegate.sh       # coder delegate unit tests (mock cursor-agent)
bash tests/test-cr-delegate.sh       # reviewer delegate unit tests (mock cursor-agent)
bash tests/test-cx-delegate.sh       # Codex reviewer delegate unit tests (mock codex)
bash tests/test-gen-changelog.sh     # changelog section generation from commit history
bash tests/test-update-changelog.sh  # idempotent changelog file writes
bash tests/test-sync-models.sh       # models.json -> doc regeneration
bash tests/test-drift-coverage.sh    # guards the dynamic --model probes
```
See `tests/e2e-smoke.md` for the manual end-to-end checks with a real cursor-agent.
