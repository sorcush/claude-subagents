# Reviewer and coder pools

Date: 2026-08-22
Status: approved design, ready for planning

## Goal

Make it easy to change which model and which command line tool the plugin uses.

Today the plugin has three fixed roles in `models.json`, three delegate scripts, three
subagents and three commands. Each of those names one tool and one model. Changing a
model means editing many files and running a code generator. Adding a tool means
writing a new script, a new subagent and a new command.

After this change, the plugin has two pools of workers. Adding a model is one entry in
one JSON file. Adding a tool is one new file with three shell functions. The
orchestrator never knows any model name; it asks the user to pick from a menu that is
built at run time from the pool files.

## Scope

In scope:

- Two pool files, `reviewers.json` and `coders.json`.
- One generic reviewer path and one generic coder path, replacing three of each.
- A harness layer, so that Cursor, Codex and Claude are interchangeable.
- A menu before every fresh delegation, built at run time.
- A worktree for the coder, so the main working folder is never modified.
- A feature branch workflow with a question at the end of the run.
- Removal of `sync-models.sh` and everything that supports it.

Out of scope:

- Making the plugin work under an orchestrator other than Claude Code. The plugin
  stays a Claude Code plugin and keeps using the `superpowers` skills and subagents.
- Renaming the plugin. It stays `cursor-subagent-cc`.
- Changing the rubrics in `rubrics/`.
- Changing the effectiveness log format, apart from one new line recording which
  worker was used.

## The pools

### `.claude-plugin/reviewers.json`

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

### `.claude-plugin/coders.json`

```json
{
  "coders": [
    { "key": "cursor-composer", "label": "Cursor Composer 2.5",  "harness": "cursor", "model": "composer-2.5", "default": true },
    { "key": "codex-luna",      "label": "Codex GPT-5.6 Luna",   "harness": "codex",  "model": "gpt-5.6-luna" }
  ]
}
```

### Field rules

| Field | Required | Rule |
|---|---|---|
| `key` | yes | Matches `^[a-z0-9][a-z0-9-]*$`. Unique inside the file. Used on the command line. |
| `label` | yes | Shown in the menu. Printable ASCII. No newline. |
| `harness` | yes | One of `cursor`, `codex`, `claude`. A file named `scripts/harness/<harness>.sh` must exist. |
| `model` | yes | Matches `^[A-Za-z0-9._+-]+$`. Passed to the tool. |
| `default` | no | `true` on at most one entry per file. The menu highlights it first. |

The order of entries in the file is the order in the menu.

### Model id confirmation

All ids above were checked on 2026-08-22.

| Id | How it was checked |
|---|---|
| `gpt-5.6-sol` (codex) | Real `codex exec` probe. Answered. |
| `gpt-5.6-luna` (codex) | Real `codex exec` probe. Answered. |
| `cursor-grok-4.6-high` | Listed by `cursor-agent models` as "Cursor Grok 4.6". |
| `gpt-5.6-sol-high` | Listed by `cursor-agent models` as "GPT-5.6 Sol 1M High". |
| `composer-2.5` | Listed by `cursor-agent models` as "Composer 2.5 (current)". |
| `claude-opus-5` | The model id used by this project's own Claude Code sessions. |

Cursor has no plain `cursor-grok-4.6` or `gpt-5.6-sol` id. Every Cursor id carries an
effort level, and most have a `-fast` twin.

**Rule for choosing Cursor ids: never use a `-fast` variant, for any provider.** Fast
variants cost more. Use the plain id at the same effort level. This is why the two
Cursor reviewers use `-high` and not `-high-fast`, even though the plugin used
`cursor-grok-4.5-high-fast` before this change.

A wrong id is caught on first use by `scripts/probe.sh`, which fails with the tool's
own error message, so a mistake here is visible immediately and is never silent.

## The harness layer

`scripts/harness/<name>.sh` is a shell file that is sourced by a delegate script. It
must define exactly three functions. Nothing outside these files knows how a tool is
invoked or what its output looks like.

### `harness_probe <model>`

Runs a very small call that asks the tool to reply with the single word `READY`.
Returns 0 if the reply is `READY`. Returns non-zero for anything else: not logged in,
workspace not trusted, timeout, wrong model id, or any other failure. Writes the real
error to standard error.

### `harness_run <mode> <model> <dir> <prompt> <session>`

Runs one call.

- `mode` is `edit` or `read-only`.
- `dir` is the working folder for the call.
- `session` is empty for a new call, or a session id to continue an earlier call.

On success it sets two shell variables and returns 0:

- `SESSION_ID` — the session id the tool reported, so a later call can continue.
- `RESULT` — the tool's final text answer.

On failure it returns non-zero and writes the reason to standard error. A call that
finishes without a final answer, or that reports an error inside its output, counts as
a failure.

### `harness_render <json-line>`

Takes one line of the tool's streaming output and writes one short human-readable
progress line to standard error. Unknown line types are ignored.

### The three implementations

| Harness | New call | Continue a call | Read-only mode |
|---|---|---|---|
| `cursor` | `cursor-agent -p --force --trust --approve-mcps --output-format stream-json --model <m>` | add `--resume=<id>` | add `--mode ask` |
| `codex` | `codex exec --json -C <dir> -m <m>` | `codex exec resume <id> --json -m <m>` | add `-s read-only` |
| `claude` | `claude -p --model <m> --output-format stream-json` | add `--resume <id>` | add `--allowedTools "Read Grep Glob" --permission-mode dontAsk` |

For `claude` in edit mode, add `--permission-mode acceptEdits`.

Only `codex` has a flag for the working folder (`-C`). The `cursor` and `claude`
harnesses must change into `<dir>` in a subshell before running their command, so that
`<dir>` is always honoured whichever harness is used.

The Claude harness starts a separate `claude -p` process. It is not an in-session
subagent. This matters: it has no memory of the orchestrator's conversation, which is
what makes it usable as an independent reviewer.

Note on the Claude reviewer: if the orchestrator session is also Opus 5, then the
reviewer is the same model as the author of the document. The review still runs in a
fresh session with no memory of writing the document, but it is not a different model.
This is accepted.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/lib/pool.sh` | Sourced library. Reads and validates a pool file. Lists entries. Looks up one entry by key. |
| `scripts/pool.sh` | `list reviewers` or `list coders`. Prints the pool as JSON so the orchestrator can build a menu. |
| `scripts/probe.sh` | `--role coder\|reviewer --key <k>`. Looks up the entry, sources its harness, runs `harness_probe`. |
| `scripts/code-delegate.sh` | `--coder <key> --task-file <f> --verify-cmd <c> --cwd <dir> [--max-retries N] [--session <id>]` |
| `scripts/review-delegate.sh` | `--reviewer <key> --target spec\|plan --doc-file <f> [--spec-file <f>] [--lenses <csv>] [--session <id>]` |
| `scripts/worktree.sh` | `prepare` and `remove`. |

Removed: `scripts/sync-models.sh`, `scripts/cc-delegate.sh`, `scripts/cr-delegate.sh`,
`scripts/cx-delegate.sh`.

### Output discipline

Every script keeps the existing rule: exactly one line of JSON on standard output, and
everything else on standard error. This is unchanged and the delegator subagents depend
on it.

`code-delegate.sh` prints:

```json
{"status":"DONE|BLOCKED","coder":"<key>","session_id":"...","attempts":N,
 "verified":true|false,"result":"...","verify_output":"..."}
```

`review-delegate.sh` prints:

```json
{"status":"REVIEWED|BLOCKED","reviewer":"<key>","session_id":"...","target":"spec|plan",
 "lenses":["backend"],"report":"...","diagnostic":"..."}
```

`worktree.sh prepare` prints:

```json
{"status":"READY","worktree":"/path/to/worktree","work_branch":"feature/x-work",
 "feature_branch":"feature/x","linked":["node_modules",".venv"]}
```

`code-delegate.sh` keeps the retry loop from the current `cc-delegate.sh`: run the
task, run the verify command, and on failure continue the same session with the failure
output, up to `--max-retries` times.

`review-delegate.sh` keeps the rubric assembly from the current `cr-delegate.sh`: the
target rubric, the lens rubrics, and the output format file are all read from
`rubrics/` and pasted into the prompt.

## Subagents

Three agent files become two. Neither names a model.

- `agents/coder-delegator.md`
- `agents/reviewer-delegator.md`

Each receives a `key` in its prompt and passes it to its script. Their existing rules
are unchanged: they have only `Bash` and `Read`, they never write code or reviews
themselves, they pass the report through without editing it, and they always report the
`session_id`.

`coder-delegator` runs git inside the worktree, using `git -C <worktree-path>`. The old
rule "never commit on main or master" is dropped, because `worktree.sh` guarantees the
work branch is never one of those.

Removed: `agents/cursor-coder-delegator.md`, `agents/cursor-reviewer-delegator.md`,
`agents/codex-reviewer-delegator.md`.

## Commands

Three command files become two: `commands/review.md` and `commands/implement-plans.md`.

Removed: `commands/cursor-review.md`, `commands/codex-review.md`,
`commands/cursor-implement-plans.md`.

### The menu

Before every fresh delegation the orchestrator:

1. Runs `scripts/pool.sh list reviewers` or `scripts/pool.sh list coders`.
2. Builds a menu from what that printed. The command file must never contain a list of
   models.
3. If the pool has four entries or fewer, uses the pop-up menu. If it has more than
   four, prints a numbered list and asks the user to type a number.

The first time a role is needed in a run, the orchestrator asks two things together:
which worker, and whether to keep that worker for the rest of the run. If the user says
to keep it, no further menu appears for that role. If not, the "which worker" question
is asked again before each fresh delegation.

A fix after a failed review is not a fresh delegation. It continues the same worker's
session, so it never shows a menu.

### `/review <spec|plan> <doc-path> [spec-path]`

1. Check the document is readable. Stop if not.
2. Show the reviewer menu.
3. Run `scripts/probe.sh --role reviewer --key <chosen>`. If it fails, tell the user
   which tool to log into and stop.
4. Choose lenses, unchanged from today: `backend` always; `frontend` when the document
   describes user interface parts; `ui` when it describes user-facing flows. Lenses are
   only used for `spec`, not for `plan`.
5. Dispatch `reviewer-delegator` with the key, target, document path, spec path if this
   is a plan review, and lenses if this is a spec review.
6. On `BLOCKED`, show the diagnostic and stop. Never invent a review.
7. Apply `superpowers:receiving-code-review` to the report, unchanged from today.
8. Write the Review Effectiveness Summary, unchanged from today, with one extra line
   naming the reviewer that was used.

### `/implement-plans <plan-path>`

1. Read the plan, and the spec it refers to.
2. Run `git rev-parse --abbrev-ref HEAD`. If it is `main` or `master`, ask the user to
   create a feature branch and stop.
3. Run `scripts/worktree.sh prepare`. Keep the worktree path from its output.
4. For each task in the plan:
   1. Show the coder menu, unless a coder was already fixed for this run.
   2. Run `scripts/probe.sh --role coder --key <chosen>`. Stop if it fails.
   3. Dispatch `coder-delegator` with the key, the task's full text, scene-setting
      context, a verify command, and the worktree path.
   4. Review the change in the worktree. This review is done by the orchestrator
      itself. Pool reviewers are not used for task reviews.
   5. If it needs changes, send the comments back to the same coder session and repeat
      from step 4. No menu.
   6. When the review passes, run `git merge --ff-only <work-branch>` in the main
      folder.
5. Run `scripts/worktree.sh remove`.
6. Ask the user: merge, open a pull request, or leave it.
7. Write the Delegation Effectiveness Summary, with one extra line per task naming the
   coder that was used.

The orchestrator runs these steps on its own. It stops for the user only where the
design says to: the menus, a failed probe, a merge that cannot fast-forward, a
`BLOCKED` result it cannot resolve, and the question at the end.

Everything else about `superpowers:subagent-driven-development` and
`superpowers:requesting-code-review` is unchanged.

## Git and the worktree

### Names

From branch `feature/login`:

- Work branch: `feature/login-work`
- Worktree folder: `../<repo-name>-feature-login-work`, next to the repository.

Slashes in the branch name become dashes in the folder name.

### `worktree.sh prepare`

Safe to run more than once.

1. Fails if the current branch is `main` or `master`.
2. Fails if the current branch already ends in `-work`, so a run cannot nest.
3. Creates the work branch from the current branch, and adds the worktree. If the work
   branch and the worktree folder both already exist, reuses them as they are. This is
   what makes a second run during the same session harmless.
4. Links the ignored files. For every item at the top level of the repository, asks
   `git check-ignore -q <item>`. If the item is ignored and exists, creates a symbolic
   link to it inside the worktree. Skips anything that already exists there. This is
   how `node_modules`, `.venv` and `.env` become available.
5. Prints its JSON line.

There is no configuration for the linking step. Git already knows which files are
ignored, so nothing needs to be set per project. In a repository with nothing ignored,
nothing is linked.

### `worktree.sh remove`

1. Checks that the work branch is an ancestor of the feature branch, meaning every
   commit has already been copied across. If it is not, refuses, prints which commits
   are missing, and exits non-zero. Nothing is deleted.
2. Removes the worktree folder.
3. Deletes the work branch.

### Moving work across

The orchestrator stays in the main folder on the feature branch and runs
`git merge --ff-only <work-branch>` after each passing task. This succeeds only when
the feature branch has no commits of its own, which is the normal case.

If the user commits to the feature branch during a run, the fast-forward fails. The
orchestrator stops and tells the user. It does not attempt a real merge, because a
conflict in the middle of a run is worse than stopping.

### End of run

The orchestrator offers three choices:

- **Merge.** Merge the feature branch into the branch the run started from.
- **Open a pull request.** Push the feature branch and run `gh pr create`. This talks
  to a remote server, so the orchestrator confirms before pushing. It requires `gh` to
  be installed and logged in; if it is not, it says so and offers the other two
  choices.
- **Leave it.** Do nothing. The feature branch stays as it is.

Nothing is pushed unless the user picks the pull request option.

## Error handling

| Situation | What happens |
|---|---|
| Pool file missing, unreadable, or not valid JSON | Script exits 2 with the file path in the message. |
| An entry is missing a required field, or a field breaks its rule | Script exits 2, naming the entry and the field. |
| Two entries share a key | Script exits 2, naming the key. |
| More than one entry marked `default` | Script exits 2. |
| `harness` names a file that does not exist | Script exits 2, naming the expected path. |
| Key given on the command line is not in the pool | Script exits 2, and lists the valid keys. |
| Probe fails | Command tells the user which tool to log into, and stops. |
| Tool call fails or returns nothing | Delegate script prints `BLOCKED` with the tool's own error in `diagnostic` or `verify_output`, and exits 1. |
| Verify command still fails after all retries | `BLOCKED` with the last verify output. |
| Empty `session_id` on a result that otherwise looks successful | Treated as `BLOCKED`. A missing session id means the tool never really ran. |
| Current branch is `main` or `master` | `worktree.sh prepare` fails and the command stops. |
| Fast-forward not possible | Orchestrator stops and reports. |
| `worktree.sh remove` finds unmerged commits | Refuses, lists them, deletes nothing. |

## Testing

Tests keep the current approach: fake programs on the `PATH`. `tests/mock-cursor-agent`
and `tests/mock-codex` already exist. A new `tests/mock-claude` is added.

| File | What it checks |
|---|---|
| `tests/test-pool.sh` | Valid file parses. Broken JSON, missing field, bad key format, bad model format, duplicate key, two defaults, unknown harness, unknown key. Listing preserves file order. |
| `tests/test-code-delegate.sh` | The coder path, run once for each of the three harnesses: success, verify failure then success on retry, retries exhausted, tool failure, empty session id. |
| `tests/test-review-delegate.sh` | The reviewer path, run once for each of the three harnesses: success, rubric and lens assembly, unknown lens rejected, missing rubric file, empty report, tool failure. |
| `tests/test-worktree.sh` | Uses a real temporary git repository. Create; run twice with no harm; refuse on `main`; refuse on a `-work` branch; link an ignored folder; skip a folder that is not ignored; refuse to remove with unmerged commits; remove cleanly when merged. |
| `tests/test-no-model-names.sh` | Reads every `label` and `model` from both pool files, and searches every file in `commands/` and `agents/` for them. Fails on any match. The search matches each whole value as one string, not word by word, because single words such as `Claude` and `Cursor` appear legitimately in those files. This is what keeps the orchestrator model-agnostic as the code changes over time. |

Removed: `tests/test-sync-models.sh`, `tests/test-drift-coverage.sh`,
`tests/test-cc-delegate.sh`, `tests/test-cr-delegate.sh`, `tests/test-cx-delegate.sh`.

`tests/e2e-smoke.md` is rewritten for the new commands and covers all four reviewers
and both coders against real tools.

## Other files

**`.claude-plugin/plugin.json`** — the description is rewritten by hand and no longer
names any model:

> Delegate implementation and independent design or plan review to a configurable pool
> of external models, run through Cursor, Codex or Claude, while Claude Code plans,
> decides and reviews.

Version becomes `2.0.0`, because the command names change.

**`.claude-plugin/marketplace.json`** — same treatment.

**`Makefile`** — the `sync-models.sh --check` step is removed from `release`. The
`models` target prints both pool files instead of `models.json`.

**`README.md`** — rewritten. It describes the pools and the harness layer, and points
readers at `reviewers.json` and `coders.json` instead of listing models in prose. All
comment markers are removed.

**Effectiveness logs** — `docs/cursor-coder/effectiveness-log.md` and
`docs/cursor-reviewer/effectiveness-log.md` keep their paths, so existing entries are
not orphaned. Each new entry gains a line naming the worker that was used.

**`.claude-plugin/models.json`** is deleted.

## Why this makes changing a model easy

- Change a model: edit one line in one JSON file.
- Add a model on a tool that already works: add one entry, four fields. It appears in
  the menu on the next run.
- Add a new tool: write `scripts/harness/<name>.sh` with three functions, add
  `tests/mock-<name>`, and add entries. Nothing else changes.
- Nothing to regenerate. The orchestrator reads the pool at run time, and
  `test-no-model-names.sh` prevents a model name from creeping back into the prompts.
