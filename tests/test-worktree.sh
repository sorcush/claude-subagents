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
# Secrets nested INSIDE a dependency tree must not reach the worktree. Before this was
# fixed, node_modules/some-pkg/.env and id_rsa.pem were both copied.
mkdir -p "$r/node_modules/some-pkg"
echo "NPM_TOKEN=shhh" > "$r/node_modules/some-pkg/.env"
echo "key"           > "$r/node_modules/some-pkg/id_rsa.pem"
echo "real code"     > "$r/node_modules/some-pkg/index.js"
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
check "node_modules was copied"          "1" "$([[ -f "$wt/node_modules/pkg/index.js" ]] && echo 1 || echo 0)"
check "node_modules is not a symlink"    "1" "$([[ ! -L "$wt/node_modules" ]] && echo 1 || echo 0)"
check "copied list names node_modules"   "1" \
  "$(echo "$out" | jq -r '.copied|index("node_modules")|if . == null then 0 else 1 end')"
check "an ignored folder off the list is skipped" "1" "$([[ ! -e "$wt/build-cache" ]] && echo 1 || echo 0)"
check ".env is never copied"             "1" "$([[ ! -e "$wt/.env" ]] && echo 1 || echo 0)"
check "nested .env is not copied"  "1" "$([[ ! -e "$wt/node_modules/some-pkg/.env" ]] && echo 1 || echo 0)"
check "nested .pem is not copied"  "1" "$([[ ! -e "$wt/node_modules/some-pkg/id_rsa.pem" ]] && echo 1 || echo 0)"
check "ordinary files still copied" "real code" "$(cat "$wt/node_modules/some-pkg/index.js" 2>/dev/null)"
check "purged count reported"      "2" "$(echo "$out" | jq -r '.purged_secrets')"

# --- remove ---
r=$(new_repo r-remove)
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
echo change > "$wt/f.txt"
git -C "$wt" add -A && git -C "$wt" commit -q -m "task 1"
out=$(cd "$r" && bash "$SCRIPT" remove 2>/dev/null)
check "refuses to remove unmerged work" "REFUSED" "$(echo "$out" | jq -r '.status')"
check "refusal emits one JSON line" "1" "$(printf '%s' "$out" | grep -c '')"
check "refusal emits valid JSON"    "1" "$(printf '%s' "$out" | jq -e . >/dev/null 2>&1 && echo 1 || echo 0)"
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
out=$(cd "$r" && bash "$SCRIPT" remove 2>/dev/null)
check "removes despite copied deps" "REMOVED" "$(echo "$out" | jq -r '.status')"
check "main checkout node_modules survives" "1" "$([[ -f "$r/node_modules/x.js" ]] && echo 1 || echo 0)"

# remove must NEVER delete a file the user created in the worktree. An earlier
# draft used `git worktree remove --force`, falling back to `rm -rf`, which
# destroyed exactly this file while still reporting REMOVED.
r=$(new_repo r-user-file)
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
echo keepme > "$wt/untracked-user-file.txt"
out=$(cd "$r" && bash "$SCRIPT" remove 2>/dev/null); rc=$?
check "refuses rather than deleting a user file" "REFUSED" "$(echo "$out" | jq -r '.status')"
check "refusal emits one JSON line" "1" "$(printf '%s' "$out" | grep -c '')"
check "refusal emits valid JSON"    "1" "$(printf '%s' "$out" | jq -e . >/dev/null 2>&1 && echo 1 || echo 0)"
check "refusal exits non-zero"                   "1"        "$([[ $rc -ne 0 ]] && echo 1 || echo 0)"
check "the user's file still exists"             "keepme"   "$(cat "$wt/untracked-user-file.txt" 2>/dev/null)"
check "the worktree was not destroyed"           "1"        "$([[ -d "$wt" ]] && echo 1 || echo 0)"

# Reuse must not lose the record of what the first prepare copied.
r=$(new_repo r-manifest)
printf 'node_modules/\n' > "$r/.gitignore"
git -C "$r" add -A && git -C "$r" commit -q -m ignore
mkdir -p "$r/node_modules" && echo lib > "$r/node_modules/x.js"
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null)
check "reuse still reports the copied folder" "1" \
  "$(echo "$out" | jq -r '.copied|index("node_modules")|if . == null then 0 else 1 end')"
out=$(cd "$r" && bash "$SCRIPT" remove 2>/dev/null)
check "remove works after a reuse" "REMOVED" "$(echo "$out" | jq -r '.status')"

# The manifest is writable by the coder through $WT/.git, so remove must never feed it
# unvalidated to rm -rf. Verified before this fix: "../VICTIM" deleted a file outside
# the worktree.
r=$(new_repo r-manifest-evil)
out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null); wt=$(echo "$out" | jq -r '.worktree')
echo "DO NOT DELETE" > "$(dirname "$wt")/VICTIM"
printf '../VICTIM\n' > "$(git -C "$wt" rev-parse --absolute-git-dir)/csc-copied"
(cd "$r" && bash "$SCRIPT" remove >/dev/null 2>&1)
check "manifest traversal is refused" "1" \
  "$([[ -f "$(dirname "$wt")/VICTIM" ]] && echo 1 || echo 0)"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
