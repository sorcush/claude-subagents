# Reviewer and coder pools

Date: 2026-08-22
Status: revised three times after independent review by Codex GPT-5.6 Sol
(two spec rounds, one plan round). No unresolved Critical findings.

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

## Success criteria

The change is done when all of these are true.

1. Adding a model to a tool that already works requires editing one JSON file and
   nothing else, and the new choice appears in the menu on the next run.
2. Adding a new tool requires one new file in `scripts/harness/`, one new fake program
   in `tests/`, and pool entries. No other file changes.
3. No file in `commands/` or `agents/` contains any model id or model label, and a test
   enforces this.
4. The coder never writes to any file inside the user's main working folder during a
   run. Its commits land on the work branch only, and reach the feature branch only
   after the orchestrator has reviewed them. Dependency folders are brought into the
   worktree as copy-on-write clones, never as symbolic links, and any symlink *inside*
   a copied folder that resolves outside the worktree is deleted. A test proves this
   rather than asserting it: it plants an absolute escaping link, a relative escaping
   link, and a safe internal link, then writes through the escape and checks the main
   checkout file is unchanged.
5. The review prompt sent to a reviewer is assembled from the same rubric files, in the
   same order, as the current `cr-delegate.sh` and `cx-delegate.sh` produce.
6. Continuing an earlier session works for all three tools, and a continued review is
   still read-only.
7. Every test listed in the Testing section passes.

## Scope

In scope:

- Two pool files, `reviewers.json` and `coders.json`.
- One generic reviewer path and one generic coder path, replacing three of each.
- A harness layer, so that Cursor, Codex and Claude are interchangeable.
- A menu before every fresh delegation, built at run time.
- A worktree for the coder, so the main working folder is never modified.
- A feature branch workflow, finished through the existing superpowers skill.
- Removal of `sync-models.sh` and everything that supports it.

Out of scope:

- Making the plugin work under an orchestrator other than Claude Code. The plugin
  stays a Claude Code plugin and keeps using the `superpowers` skills and subagents.
- Renaming the plugin. It stays `cursor-subagent-cc`.
- Changing the rubrics in `rubrics/`.
- Changing the effectiveness log format, apart from one new line recording which
  worker was used.
- Keeping the old command names alive as aliases. The three old commands are removed
  outright. This is a deliberate breaking change, recorded in the CHANGELOG and in the
  major version bump. Keeping a name like `/cursor-review` would preserve exactly the
  tool-specific naming this change exists to remove.

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

### Schema

The whole file must be a JSON object with exactly one key: `reviewers` for the reviewer
file, `coders` for the coder file. Its value must be an array with at least one entry.
Every entry must be an object. Any unknown key inside an entry is an error, so that a
typo such as `lable` fails loudly instead of being ignored.

| Field | Required | Rule |
|---|---|---|
| `key` | yes | String matching `^[a-z0-9][a-z0-9-]*$`. Unique inside the file. Used on the command line. |
| `label` | yes | Non-empty string. Printable ASCII only. No newline. Shown in the menu. |
| `harness` | yes | String matching `^[a-z][a-z0-9-]*$`. A file named `scripts/harness/<harness>.sh` must exist. |
| `model` | yes | String matching `^[A-Za-z0-9._+-]+$`. Passed to the tool. |
| `default` | no | Boolean. `true` on at most one entry per file. |

The `harness` value is checked by testing whether its file exists, not against a fixed
list of names. This is what makes adding a tool a one-file change. The character rule
prevents a crafted parent-directory traversal value from building a path outside the
plugin.

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
must define these three public functions; it may define private helpers alongside
them. Nothing outside these files knows how a tool is invoked or what its output
looks like.

### Calling rules

These rules apply to every harness file and are as much a part of the contract as the
function names.

- **`harness_run` must be called as a plain statement, never inside `$(...)` or a
  pipeline.** It reports its results by setting shell variables, and a command
  substitution or pipeline would run it in a subshell where those assignments are
  thrown away. The current `cc-delegate.sh` carries a comment describing exactly this
  trap, which is why it returns its result through `echo` instead.
- **A harness function must write nothing to standard output.** Progress and errors go
  to standard error. Standard output belongs to the calling script's single JSON line.
- Each harness reads its tool's streaming output itself and is responsible for
  detecting an error reported inside that output, not only a non-zero exit code.

### `harness_probe <model>`

Runs a very small call that asks the tool to reply with the single word `READY`.
Returns 0 if the reply is `READY`. Returns non-zero for anything else. Writes the real
error text to standard error, and writes a one-word classification to the variable
`PROBE_REASON`, one of: `auth`, `trust`, `not-installed`, `bad-model`, `timeout`,
`other`. The calling command uses this to give the right advice instead of always
telling the user to log in.

### `harness_run <mode> <model> <dir> <prompt> <session>`

Runs one call.

- `mode` is `edit` or `read-only`.
- `dir` is the working folder for the call. It is resolved to an absolute path by the
  caller before this function is entered.
- `session` is empty for a new call, or a session id to continue an earlier call.

On success it sets two shell variables and returns 0:

- `SESSION_ID` — the session id the tool reported, so a later call can continue.
- `RESULT` — the tool's final text answer.

On failure it returns non-zero and writes the reason to standard error. A call that
finishes without a final answer, or that reports an error inside its output, counts as
a failure.

`mode` and `dir` must hold for **every** call, including a continued one. This is not
automatic; see the resume rules below.

### `harness_render <json-line>`

Takes one line of the tool's streaming output and writes one short human-readable
progress line to standard error. Unknown line types are ignored.

### The three implementations

| Harness | New call | Continue a call |
|---|---|---|
| `cursor` | `cursor-agent -p --force --trust --approve-mcps --output-format stream-json --model <m>` | add `--resume=<id>` |
| `codex` | `codex exec --json -C <dir> -m <m>` | `codex exec resume <id> --json -m <m>` |
| `claude` | `claude -p --model <m> --output-format stream-json` | add `--resume <id>` |

Read-only mode adds: `--mode ask` for `cursor`; `-s read-only` for `codex`;
`--allowedTools "Read Grep Glob" --permission-mode dontAsk` for `claude`.
Edit mode adds `--permission-mode acceptEdits` for `claude`.

**Working folder.** Only `codex` has a flag for it, and only on a new call. The
`cursor` and `claude` harnesses, and the `codex` resume path, must change into `<dir>`
in a subshell before running their command.

**Resume and the read-only sandbox.** This was checked against the real CLI on
2026-08-22: `codex exec resume` accepts `-m`, but has **no `-C`** and **no
`-s`/`--sandbox`**. A resumed review would therefore fall back to whatever sandbox the
user's `~/.codex/config.toml` sets, silently losing read-only enforcement. `resume`
does accept `-c`, so the codex harness must pass
`-c sandbox_mode="read-only"` on every resumed read-only call. A test asserts this flag
is present. The same care applies to any tool added later: if a resumed call cannot be
guaranteed to keep its mode and folder, the harness must refuse to resume rather than
run unsafely.

**Timeouts.** No call may run without a limit. macOS has no `timeout` program by
default, so `scripts/lib/timeout.sh` provides a small portable helper. Defaults: 120
seconds for a probe, 1800 seconds for a run. Both can be overridden by the environment
variables `CSC_PROBE_TIMEOUT` and `CSC_RUN_TIMEOUT`. A call stopped by the timeout is a
failure with reason `timeout`.

A timed-out call returns exit status **124**, the convention GNU `timeout` uses. This
matters because the delegates invoke the helper inside `( cd "$dir" && ... )`: a shell
variable set in that subshell is discarded when it exits, but an exit status crosses
the boundary. Reporting a timeout only through a variable would mean the caller could
never distinguish a timeout from any other failure.

The helper must terminate the whole **process group**, not just the process it started.
Killing only the wrapper would leave the underlying tool running, and an edit-capable
tool could still be changing files after the delegate has already reported `BLOCKED`.
So the helper starts the command in its own process group with `set -m`, sends `TERM`
to the negated group id, waits a short grace period, then sends `KILL` to anything
still alive. It also installs a trap so that an interrupted caller cleans the group up
rather than orphaning it, and **restores the caller's own `INT` and `TERM` traps
afterwards** rather than clearing them, so it cannot silently disarm the calling
script's handlers.

### A note on the Claude reviewer

The Claude harness starts a separate `claude -p` process. It is not an in-session
subagent. This matters: it has no memory of the orchestrator's conversation, which is
what makes it usable as an independent reviewer.

If the orchestrator session is also Opus 5, then the reviewer is the same model as the
author of the document. The review still runs in a fresh session with no memory of
writing the document, but it is not a different model. This is accepted.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/lib/pool.sh` | Sourced library. Reads and validates a pool file. Lists entries. Looks up one entry by key. |
| `scripts/lib/timeout.sh` | Sourced library. Portable bounded execution helper. |
| `scripts/pool.sh` | `list reviewers` or `list coders`. Prints the pool as JSON so the orchestrator can build a menu. |
| `scripts/probe.sh` | `--role coder\|reviewer --key <k>`. Looks up the entry, sources its harness, runs `harness_probe`. |
| `scripts/code-delegate.sh` | `--coder <key> --task-file <f> --verify-cmd <c> --cwd <dir> [--max-retries N] [--session <id>]` |
| `scripts/review-delegate.sh` | `--reviewer <key> --target spec\|plan --doc-file <f> [--spec-file <f>] [--lenses <csv>] [--session <id>]` |
| `scripts/worktree.sh` | `prepare` and `remove`. |

Removed: `scripts/sync-models.sh`, `scripts/cc-delegate.sh`, `scripts/cr-delegate.sh`,
`scripts/cx-delegate.sh`.

### Output discipline

Every script keeps the existing rule: exactly one line of JSON on standard output, and
everything else on standard error.

`pool.sh list <role>`:

```json
{"role":"reviewers","entries":[{"key":"...","label":"...","harness":"...","default":true}]}
```

The `model` field is deliberately left out, so that a model id cannot reach the
orchestrator's context through the menu.

`probe.sh`:

```json
{"status":"READY|FAILED","role":"reviewer","key":"...","reason":"auth|trust|not-installed|bad-model|timeout|other|","diagnostic":"..."}
```

`code-delegate.sh`:

```json
{"status":"DONE|BLOCKED","coder":"<key>","session_id":"...","attempts":N,
 "verified":true|false,"changed":true|false,"commit_id":"...","result":"...","verify_output":"..."}
```

`review-delegate.sh`:

```json
{"status":"REVIEWED|BLOCKED","reviewer":"<key>","session_id":"...","target":"spec|plan",
 "lenses":["backend"],"report":"...","diagnostic":"..."}
```

`worktree.sh prepare`:

```json
{"status":"READY","worktree":"/abs/path","work_branch":"feature/x-work",
 "feature_branch":"feature/x","copied":["node_modules"],"skipped":[],
 "neutralized_symlinks":0,"clone_supported":true}
```

`worktree.sh remove`:

```json
{"status":"REMOVED|REFUSED","work_branch":"...","unmerged":["<sha> <subject>"],"diagnostic":"..."}
```

### Where the coder's work happens

`code-delegate.sh` resolves `--cwd` to an absolute path and fails if it is not a
directory or not a git worktree. Then **every** step runs inside that folder:

- the first call to the coder,
- every retry call,
- the verify command,
- any `git` command, always written as `git -C <cwd>`.

This must be stated in the implementation, not assumed. Today `cc-delegate.sh` runs
`eval "$VERIFY_CMD"` in whatever folder it was called from. Carrying that behaviour
across unchanged would verify the main checkout instead of the worktree, which would
pass or fail for the wrong reasons and is the single most likely mistake in this work.

`code-delegate.sh` keeps the retry loop from the current `cc-delegate.sh`: run the
task, run the verify command, and on failure continue the same session with the failure
output, up to `--max-retries` times.

`review-delegate.sh` keeps the rubric assembly from the current `cr-delegate.sh`: the
target rubric, the lens rubrics, and the output format file are all read from
`rubrics/` and pasted into the prompt, in that order.

## Subagents

Three agent files become two. Neither names a model.

- `agents/coder-delegator.md`
- `agents/reviewer-delegator.md`

Each receives a `key` in its prompt and passes it to its script. Their existing rules
are unchanged: they have only `Bash` and `Read`, they never write code or reviews
themselves, they pass the report through without editing it, and they always report the
`session_id`.

### Committing

`coder-delegator` commits, as it does today, but inside the worktree.

1. It runs `git -C <worktree> rev-parse --abbrev-ref HEAD` and stops if the branch is
   not the expected work branch.
2. It runs `git -C <worktree> status --porcelain`. If the output is empty, the coder
   changed nothing: it reports `DONE` with `changed:false` and makes no commit. The
   orchestrator then treats the fast-forward as a no-op and moves on.
3. Otherwise it runs `git -C <worktree> add -A && git -C <worktree> commit -m "..."`.
   If the commit fails, that is a `BLOCKED` result with the git error, never a silent
   success.
4. It reports the resulting commit id in the `commit_id` field of the script's JSON
   line, and repeats it in its own report to the orchestrator. That commit is what the
   orchestrator reviews and what the fast-forward moves across. When `changed` is
   `false`, `commit_id` is an empty string.

The old rule "never commit on main or master" is replaced by step 1, which is stricter:
the branch must be the specific work branch for this run.

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

The menu shows entries in file order, always, in both the pop-up and the numbered
list. It never reorders them. The entry marked `default` is marked in place as
recommended; it is not moved to the top. The user must still choose. If no entry is
marked `default`, none is marked as recommended and nothing else changes. If the pool is empty, that is a
validation error from `pool.sh`, and the command stops.

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
   what is wrong based on the `reason` field, and stop.
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
3. Run `git status --porcelain`. If there are uncommitted or staged changes, stop and
   ask the user to commit or stash them. A worktree is created from the last commit, so
   uncommitted work would be invisible to the coder and would still be sitting in the
   way when the fast-forward happens later.
4. Run `scripts/worktree.sh prepare`. Keep the worktree path from its output.
5. For each task in the plan:
   1. Show the coder menu, unless a coder was already fixed for this run.
   2. Run `scripts/probe.sh --role coder --key <chosen>`. Stop if it fails.
   3. Dispatch `coder-delegator` with the key, the task's full text, scene-setting
      context, a verify command, and the worktree path.
   4. Review the change in the worktree. This review is done by the orchestrator
      itself. Pool reviewers are not used for task reviews.
   5. If it needs changes, send the comments back to the same coder session and repeat
      from step 4. No menu.
   6. When the review passes, run `git merge --ff-only <work-branch>` in the main
      folder. If the task changed nothing, this is a no-op and is not an error.
6. Run `scripts/worktree.sh remove`.
7. Follow `superpowers:finishing-a-development-branch`, exactly as the current command
   already does. That skill checks the tests pass, works out the base branch itself,
   and offers merge, push and open a pull request, keep the branch, or discard. This
   design does not reimplement any of that.
8. Write the Delegation Effectiveness Summary, with one extra line per task naming the
   coder that was used.

The orchestrator runs these steps on its own. It stops for the user only where the
design says to: the menus, a failed probe, a dirty working folder, a merge that cannot
fast-forward, a `BLOCKED` result it cannot resolve, and the questions that
`finishing-a-development-branch` asks at the end.

Everything else about `superpowers:subagent-driven-development` and
`superpowers:requesting-code-review` is unchanged.

## Git and the worktree

### Names

From branch `feature/login`:

- Work branch: `feature/login-work`
- Worktree folder: `../<repo-name>-feature-login-work`, next to the repository.

Slashes in the branch name become dashes in the folder name. If the resulting folder
name already exists and is not this run's worktree, `prepare` refuses rather than
guessing.

### `worktree.sh prepare`

Safe to run more than once, because it verifies before it reuses.

1. Fails if the current branch is `main` or `master`.
2. Fails if the current branch already ends in `-work`, so a run cannot nest.
3. Decides whether to create or reuse:
   - **Neither exists** — creates the work branch from the current commit, and adds the
     worktree.
   - **Both exist** — reuses them only if all of these hold. Otherwise refuses and
     prints which check failed:
     - `git worktree list --porcelain` shows the folder registered to this repository
       and checked out on the work branch;
     - `git -C <worktree> status --porcelain` is empty, so no uncommitted work is left
       from an earlier run;
     - the work branch points at **exactly the same commit** as the feature branch.

     The last check is the strict one, and it replaces a weaker "is an ancestor of"
     test from an earlier draft. A work branch that is merely clean and descended from
     the feature branch may still be carrying commits from an abandoned earlier run,
     which the very first fast-forward of the new run would pull in as if this run had
     produced them. When the work branch is ahead, `prepare` refuses and lists the extra
     commits, so the user can either merge them deliberately or delete the branch. It
     never decides that on its own.
   - **Only one exists** — refuses. A branch without its worktree, or a folder without
     its branch, is a leftover from an interrupted run and needs a person to look at
     it.
4. Links the dependency folders, described below.
5. Prints its JSON line.

### Copying dependency folders

A worktree contains only tracked files, so an installed dependency folder is missing
and a verify command such as `npm test` would fail for reasons that have nothing to do
with the code.

`prepare` brings across a fixed built-in list of dependency folders. A folder is taken
only if it is at the top level of the repository, exists, is ignored by git, and is on
the list:

```
node_modules  .venv  venv  vendor  .bundle  target  .gradle  .m2  .tox  .cargo
```

Nothing is taken if it matches any of these patterns, even if it is also on the list:

```
.env*  *.pem  *.key  *.p12  credentials*  .netrc  .npmrc  .aws  .ssh
```

**They are copied, not linked, using a copy-on-write clone.** On macOS this is
`cp -Rc`; on Linux it is `cp --reflink=auto`. A clone shares the underlying disk blocks,
so it costs almost no space and very little time. When the coder writes to a file
inside the clone, the filesystem copies just that one file, and the original in the
main checkout is untouched.

**Copying is not enough on its own: escaping symlinks must be removed too.** `cp -R`
and `cp -Rc` both *preserve* symbolic links rather than following them. A dependency
tree can contain a link pointing outside itself — monorepo package managers create
these routinely, and some are absolute paths. Such a link survives the copy and still
points at the main checkout, so writing through it in the worktree changes the real
file. This was verified on 2026-08-22: an absolute link inside a copied `node_modules`
let a write in the worktree overwrite a file in the main checkout.

So after copying a folder, `prepare` walks every symlink inside it and resolves the
target. A link resolving inside the worktree is kept — `node_modules/.bin` entries
point within `node_modules` and are needed for tests to run. A link resolving anywhere
else is deleted, and the count is reported in the `neutralized_symlinks` field.

Clone support is probed between the **actual** source and destination directories, not
in a temporary directory. A clone only works within one filesystem, so a probe done
elsewhere can report success while the real copy quietly falls back to a full copy and
bypasses the size guard below.

This was measured on 2026-08-22: `cp -Rc` cloned 500 files in 0.27 seconds, and writing
to a file in the clone left the source file unchanged.

If the filesystem does not support cloning, `cp` falls back to a real copy on its own.
`prepare` therefore counts the entries in a folder first. Under 5000 entries it copies
anyway. At or above 5000 it skips the folder, records it in the `skipped` field of its
output, and the orchestrator warns the user that a verify command may fail for a
missing dependency. It never silently spends minutes copying a large folder.

**Why not symbolic links.** Two earlier drafts of this design got this wrong, and the
reason is worth writing down. The first linked **everything** git ignores. The second
narrowed the list but still used links. Both were unsafe for the same reason: a
symbolic link is a two-way door. The coder has edit permission, so a link to your real
`node_modules` lets it write into your main working folder, and an `npm install` in the
worktree would change your actual dependencies. That directly contradicts success
criterion 4. A copy-on-write clone gives the same speed with none of that exposure.

Reading secrets is a separate and smaller concern. It is not new, because the coder
already runs inside your repository today and can already read `.env`. The denylist
above reduces it anyway, at no cost.

If a verify command still fails because something is missing, the orchestrator says so
and asks the user once what to run. There is no configuration file.

### `worktree.sh remove`

1. Checks that the work branch is an ancestor of the feature branch, meaning every
   commit has already been copied across. If it is not, refuses, prints the missing
   commits, and exits non-zero. Nothing is deleted.
2. Deletes only the dependency folders that this run created, named from the manifest
   `prepare` wrote. It never deletes anything else in the worktree. This step is
   required, not cosmetic: `git worktree remove` refuses to run while untracked files
   are present, and the copied dependencies are untracked.
3. Runs `git worktree remove` **without `--force`, and with no `rm -rf` fallback.**
   If anything untracked remains, that is a file the user created and this script did
   not; it returns `REFUSED` with the diagnostic and deletes nothing further. An
   earlier draft used `--force` with an `rm -rf` fallback and still reported
   `REMOVED`, which would have silently destroyed the user's own files.
4. Deletes the work branch with `git branch -d`, not `-D`. The safety that makes this
   sufficient is step 1, which already proved nothing would be lost.
5. Every one of these steps is checked. A failure returns `REFUSED` with what remains,
   never `REMOVED`.

An earlier draft justified this cleanup by claiming `git worktree remove` might follow
a symbolic link out of the worktree. That was wrong; it does not. The real reason is
the untracked-file refusal in step 2, and the rule that this script only ever deletes
what it created.

### Moving work across

The orchestrator stays in the main folder on the feature branch and runs
`git merge --ff-only <work-branch>` after each passing task.

The condition for this to succeed is that **the feature branch is an ancestor of the
work branch**. It is not "the feature branch has no commits of its own": after the
first task it does have commits, because the first fast-forward put them there. The
`--ff-only` flag enforces the correct condition by itself, so no extra check is needed,
but the implementation must not encode the wrong one.

If the user commits to the feature branch during a run, it stops being an ancestor and
the fast-forward fails. The orchestrator stops and tells the user. It does not attempt
a real merge, because a conflict in the middle of a run is worse than stopping.

## Error handling

| Situation | What happens |
|---|---|
| Pool file missing, unreadable, or not valid JSON | Exit 2 with the file path in the message. |
| Top-level key wrong, value not an array, or array empty | Exit 2, naming the expected key. |
| An entry is not an object, is missing a field, has an unknown field, or breaks a field rule | Exit 2, naming the entry position and the field. |
| Two entries share a key | Exit 2, naming the key. |
| More than one entry marked `default`, or `default` is not a boolean | Exit 2. |
| `harness` names a file that does not exist | Exit 2, naming the expected path. |
| Key given on the command line is not in the pool | Exit 2, and list the valid keys. |
| Probe fails | Command reports the tool's own error and gives advice matching `reason`: log in for `auth`, trust the workspace for `trust`, install the tool for `not-installed`, check the pool file for `bad-model`. It never says "log in" for a failure that is not an authentication failure. |
| Tool call fails or returns nothing | `BLOCKED` with the tool's own error in `diagnostic` or `verify_output`. Exit 1. |
| A probe exceeds its timeout | The whole process group is terminated. `probe.sh` prints `FAILED` with reason `timeout`. |
| A delegate call exceeds its timeout | The whole process group is terminated. The delegate prints `BLOCKED` with reason `timeout`. Probe failures and delegate failures use different words on purpose: a probe never returns `BLOCKED`, and a delegate never returns `FAILED`. |
| Verify command still fails after all retries | `BLOCKED` with the last verify output. |
| Empty `session_id` on a result that otherwise looks successful | Treated as `BLOCKED`. A missing session id means the tool never really ran. |
| Current branch is `main`, `master`, or ends in `-work` | `prepare` fails and the command stops. |
| Working folder has uncommitted changes | Command stops before creating the worktree and asks the user to commit or stash. |
| Worktree or work branch exists but fails a reuse check | `prepare` refuses, naming the failed check. Nothing is deleted or overwritten. |
| Work branch is ahead of the feature branch on reuse | `prepare` refuses and lists the extra commits. The user decides whether to merge or delete them. |
| A dependency folder has 5000 entries or more and cloning is unavailable | The folder is skipped and named in `skipped`. The orchestrator warns that a verify command may fail for a missing dependency. |
| Fast-forward not possible | Orchestrator stops and reports. |
| `remove` finds unmerged commits | Refuses, lists them, deletes nothing. |
| Coder changed no files | `DONE` with `changed:false`. No commit. The fast-forward is a no-op. |
| Coder commit fails | `BLOCKED` with the git error. |

Failures at the end of a run — no remote, a push that succeeds but a pull request that
does not, a pull request that already exists — are handled by
`superpowers:finishing-a-development-branch`, not by this design.

## Testing

Tests keep the current approach: fake programs on the `PATH`. `tests/mock-cursor-agent`
and `tests/mock-codex` already exist. A new `tests/mock-claude` is added.

| File | What it checks |
|---|---|
| `tests/test-pool.sh` | Valid file parses and preserves order. Broken JSON, wrong top-level key, empty array, entry not an object, missing field, unknown field, bad key format, bad model format, bad harness format, harness file absent, duplicate key, two defaults, non-boolean default, unknown key on the command line. A `harness` value containing `..` is rejected. The listing output contains no `model` field. |
| `tests/test-code-delegate.sh` | Run once for each of the three fake tools: success; verify fails then succeeds on retry; retries exhausted; tool failure; empty session id; no changes made. Also: `--cwd` that does not exist is rejected; the verify command runs in `--cwd` and not in the caller's folder; every git call carries `-C`. |
| `tests/test-review-delegate.sh` | Run once for each of the three fake tools: success; rubric and lens files assembled in the right order; unknown lens rejected; missing rubric file; empty report; tool failure. Also: the codex resume path includes `-c sandbox_mode="read-only"`, and the cursor and claude resume paths keep their read-only flags. |
| `tests/test-worktree.sh` | Uses a real temporary git repository. Create; run twice with no harm; refuse on `main`; refuse on a branch ending in `-work`; refuse when only the branch exists; refuse when only the folder exists; refuse when the worktree is dirty; refuse when the folder name is taken by something else; **refuse when the work branch is clean but ahead of the feature branch, and list the extra commits**; copy a folder on the list; do not copy a folder that is ignored but not on the list; do not copy `.env`; skip a folder of 5000 entries or more and report it in `skipped`; refuse to remove with unmerged commits; remove deletes only the folders it created and leaves other untracked files alone; remove succeeds even though the copied dependencies were untracked. |
| `tests/test-worktree-isolation.sh` | The guarantee in success criterion 4, proved rather than asserted. Creates a repository with an ignored dependency folder, runs `prepare`, writes to a file inside the copied folder in the worktree, and checks the source file in the main checkout is unchanged. Also checks no entry in the worktree's dependency folder is a symbolic link pointing outside the worktree. |
| `tests/test-timeout.sh` | The portable helper terminates a command that runs too long and returns a failure, and does not disturb a command that finishes in time. It also kills a **child** of the timed-out command, not only the command itself: the test starts a script that spawns a long-running child, lets it time out, and asserts the child is gone. |
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

**`CHANGELOG.md`** — records the removal of the three old commands as a breaking
change, and names the two commands that replace them.

**Effectiveness logs** — `docs/cursor-coder/effectiveness-log.md` and
`docs/cursor-reviewer/effectiveness-log.md` keep their paths, so existing entries are
not orphaned. Each new entry gains a line naming the worker that was used.

**`.claude-plugin/models.json`** is deleted.

## Review history

Two rounds with Codex GPT-5.6 Sol, spec and lens-backend rubrics, the second resuming
the first reviewer's own session so it could grade its earlier findings.

### Round one — verdict: needs revision (5 Critical, 11 Important, 4 Minor)

All five Critical findings were accepted and fixed: the undefined base branch (fixed by
using `superpowers:finishing-a-development-branch` rather than a hand-written flow),
unsafe worktree reuse, over-broad linking of ignored files, the unstated working folder
for verification, and the loss of the read-only sandbox on a resumed Codex call. Every
Important and Minor finding was also accepted.

One finding was rejected: the suggestion to keep the old command names as aliases. This
plugin has a single author and user, and keeping a name such as `/cursor-review` would
preserve the tool-specific naming this change exists to remove. The major version bump
and the CHANGELOG entry are the migration path. **The reviewer accepted this reasoning
in round two.**

One finding was accepted with a correction to its reasoning: the over-broad linking was
described as exposing secrets unnecessarily. Read access to `.env` is not new, because
the coder already runs inside the repository today. The new and genuinely wrong part
was the write path back into the main checkout. **The reviewer agreed with this
correction in round two**, while correctly noting that agreeing with it did not make
the write path acceptable.

### Round two — verdict: needs revision (1 new Critical, 4 Important, 2 Minor)

Of the twenty round-one findings, sixteen were graded FIXED and four PARTIALLY FIXED.

The new Critical finding was a genuine self-contradiction: success criterion 4 promised
the coder never writes into the main working folder, while the linking section handed
it writable symbolic links to `node_modules` and build caches. Both could not be true.
Resolved by cloning with copy-on-write (`cp -Rc` on macOS, `cp --reflink=auto` on
Linux) instead of linking, which keeps the promise true and was measured at 500 files
in 0.27 seconds. A test now proves the guarantee instead of asserting it.

The four partially-fixed findings and the remaining new ones were all accepted: worktree
reuse now requires the work branch to point at exactly the same commit as the feature
branch, rather than merely being a clean descendant that might carry an abandoned run's
commits; the menu no longer both preserves file order and moves the default to the top;
the timeout helper now kills the whole process group, so a timed-out tool cannot keep
editing after the delegate has reported `BLOCKED`; the cleanup step's justification was
corrected, because `git worktree remove` does not follow symbolic links, and the real
reason is its refusal to run while untracked files are present; `commit_id` was added to
the delegate's output contract; and probe timeouts now report `FAILED` while delegate
timeouts report `BLOCKED`, which the two schemas had disagreed about.

### Note on how round two was run

The current `cx-delegate.sh` resume path is the one round one found to be unsafe: it
omits `-s read-only`, and this user's `~/.codex/config.toml` marks `/Users/sandrey/Dev`
as trusted with no `sandbox_mode` set, so a resumed reviewer would have had write
access to this repository. Round two was therefore run with the fix this spec
prescribes, `-c sandbox_mode="read-only"`, applied by hand. It worked: the reviewer
resumed with full context and made no writes. The fix is confirmed in practice, not
only on paper.

### Round three — the plan review, and what it sent back into this spec

The implementation plan (`docs/superpowers/plans/2026-08-22-reviewer-coder-pools.md`)
was reviewed against this spec. Verdict: needs revision, with 4 Critical findings.
Three of those were defects in the plan's code alone, but one exposed a hole in *this*
document, so it is recorded here.

**The copy-on-write clone did not, by itself, deliver the isolation this spec
promises.** `cp -R` and `cp -Rc` preserve symbolic links instead of following them, so
a dependency tree containing a link that points outside itself still carries that link
into the worktree. Verified on 2026-08-22 by planting an absolute link inside a copied
`node_modules` and writing through it from the worktree: the file in the main checkout
changed. Success criterion 4 and the copying section now require every symlink inside
a copied folder to be resolved, with anything escaping the worktree deleted.

This is the third consecutive round in which the same guarantee leaked, by a different
mechanism each time: link everything ignored, then link a narrower list, then copy but
keep the links inside. The lesson recorded for the rubrics is that a stated guarantee
needs a test that attacks it, not a description that restates it.

Two smaller corrections also came back into this spec. `worktree.sh remove` must not
use `--force` or fall back to `rm -rf`, because both would delete files the user
created in the worktree while still reporting success; it now refuses instead. And the
timeout helper must report a timeout through exit status 124 rather than only a shell
variable, because the delegates call it inside a subshell where a variable assignment
is discarded — the same subshell trap this spec already warns about for `harness_run`,
one level further down.

One plan finding was rejected: the reviewer called `out="$(cmd)"` followed by
`[[ $? -eq 0 ]]` a Critical correctness bug. It is not; bash sets `$?` from the command
substitution, verified directly. The plan adopts the explicit form anyway, because
misreading a failed verification as a pass is the worst failure that script could have
and the pattern breaks if anyone inserts a line between the two. The severity was
wrong; the advice was still worth taking.
