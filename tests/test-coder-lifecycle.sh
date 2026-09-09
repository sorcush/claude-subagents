#!/usr/bin/env bash
# Unit tests for scripts/lib/coder-lifecycle.sh. Run: bash tests/test-coder-lifecycle.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../scripts/lib/coder-lifecycle.sh"

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

MAIN="$TMP/main"
WT="$TMP/task-work"
WT_TWO="$TMP/task-two"
WT_MAIN="$TMP/main-work"
git init -q -b main "$MAIN"
git -C "$MAIN" config user.email test@example.com
git -C "$MAIN" config user.name Test
echo base > "$MAIN/base.txt"
git -C "$MAIN" add base.txt
git -C "$MAIN" commit -q -m base
git -C "$MAIN" switch -q -c feature/safety
git -C "$MAIN" worktree add -q -b feature/safety-work "$WT"
git -C "$MAIN" worktree add -q -b feature/other-work "$WT_TWO"
git -C "$MAIN" worktree add -q "$WT_MAIN" main

if [[ ! -r "$LIB" ]]; then
  echo "FAIL - lifecycle library is missing"
  exit 1
fi
# shellcheck source=../scripts/lib/coder-lifecycle.sh
source "$LIB"

lifecycle_open "$WT" c-codex ""
rc=$?
first_id="$LIFECYCLE_ID"
check "first owner acquires the worktree" "0" "$rc"
check "lifecycle id is generated" "1" \
  "$([[ "$first_id" =~ ^[0-9]+-[0-9]+-[0-9]+$ ]] && echo 1 || echo 0)"
check "live lock exists" "1" "$([[ -d "$LIFECYCLE_LOCK_DIR" ]] && echo 1 || echo 0)"

( source "$LIB"; lifecycle_open "$WT" c-cursor "" ) >/dev/null 2>&1
check "second owner is refused" "1" "$?"

( source "$LIB"; lifecycle_open "$WT_TWO" c-cursor ""; lifecycle_finish ) >/dev/null 2>&1
check "different worktree has an independent lock" "0" "$?"

lifecycle_finish
check "finish removes live lock" "0" "$([[ -d "$LIFECYCLE_LOCK_DIR" ]] && echo 1 || echo 0)"
check "finish removes recovery state" "0" "$([[ -e "$LIFECYCLE_STATE_FILE" ]] && echo 1 || echo 0)"

echo dirty > "$WT/dirty file.txt"
( source "$LIB"; lifecycle_open "$WT" c-codex "" ) >/dev/null 2>&1
check "fresh dirty worktree is refused" "1" "$?"
rm "$WT/dirty file.txt"

lifecycle_open "$WT" c-codex ""
blocked_id="$LIFECYCLE_ID"
echo partial > "$WT/partial file.txt"
lifecycle_update recoverable sess-1 "" 1 "verification failed"
lifecycle_block_recoverable "verification failed"
check "recoverable block releases live lock" "0" \
  "$([[ -d "$LIFECYCLE_LOCK_DIR" ]] && echo 1 || echo 0)"
check "recoverable block retains state" "1" \
  "$([[ -f "$LIFECYCLE_STATE_FILE" ]] && echo 1 || echo 0)"

( source "$LIB"; lifecycle_open "$WT" c-codex 1-2-3 ) >/dev/null 2>&1
check "wrong lifecycle cannot resume" "1" "$?"
( source "$LIB"; lifecycle_open "$WT" c-cursor "$blocked_id" ) >/dev/null 2>&1
check "different coder cannot resume" "1" "$?"

lifecycle_open "$WT" c-codex "$blocked_id"
check "matching lifecycle resumes" "0" "$?"
check "resume preserves partial file" "partial" "$(cat "$WT/partial file.txt")"
rm "$WT/partial file.txt"
lifecycle_finish

( source "$LIB"; lifecycle_open "$MAIN" c-codex "" ) >/dev/null 2>&1
check "primary checkout is refused" "1" "$?"
( source "$LIB"; lifecycle_open "$WT_MAIN" c-codex "" ) >/dev/null 2>&1
check "linked main branch is refused" "1" "$?"

# A killed owner leaves a stale lock. Recovery may remove only that lock after
# proving the recorded process is gone and the worktree state is unchanged.
owner_id_file="$TMP/owner-id"
(
  source "$LIB"
  lifecycle_open "$WT" c-codex ""
  printf '%s\n' "$LIFECYCLE_ID" > "$owner_id_file"
  lifecycle_update quarantined sess-stale "" 0 "interrupted"
  sleep 300
) &
owner_pid=$!
for _ in $(seq 1 50); do
  [[ -s "$owner_id_file" ]] && break
  sleep 0.1
done
stale_id=$(cat "$owner_id_file" 2>/dev/null || echo "")

( source "$LIB"; lifecycle_recover "$WT" "$stale_id" ) >/dev/null 2>&1
check "recovery refuses while owner is alive" "1" "$?"

kill -KILL "$owner_pid" 2>/dev/null
wait "$owner_pid" 2>/dev/null
before_hash=$(git -C "$WT" status --porcelain=v1 | shasum -a 256 | awk '{print $1}')

lifecycle_recover "$WT" 1-2-3 >/dev/null 2>&1
check "recovery rejects the wrong lifecycle id" "1" "$?"

echo drift > "$WT/recovery-drift.txt"
lifecycle_recover "$WT" "$stale_id" >/dev/null 2>&1
check "recovery rejects changed-path drift" "1" "$?"
rm "$WT/recovery-drift.txt"

git -C "$WT" switch -q -c feature/recovery-drift
lifecycle_recover "$WT" "$stale_id" >/dev/null 2>&1
check "recovery rejects branch drift" "1" "$?"
git -C "$WT" switch -q feature/safety-work
git -C "$WT" branch -D feature/recovery-drift >/dev/null

git -C "$WT" commit -q --allow-empty -m drift
lifecycle_recover "$WT" "$stale_id" >/dev/null 2>&1
check "recovery rejects commit drift" "1" "$?"
git -C "$WT" reset --hard -q HEAD^

lifecycle_recover "$WT" "$stale_id"
check "recovery accepts a stopped stale owner" "0" "$?"
after_hash=$(git -C "$WT" status --porcelain=v1 | shasum -a 256 | awk '{print $1}')
check "recovery preserves worktree files" "$before_hash" "$after_hash"
check "recovery removes stale live lock" "0" \
  "$([[ -d "$LIFECYCLE_LOCK_DIR" ]] && echo 1 || echo 0)"
check "recovery retains resumable state" "recoverable" \
  "$(jq -r '.state' "$LIFECYCLE_STATE_FILE")"

lifecycle_open "$WT" c-codex "$stale_id"
lifecycle_finish

lifecycle_open "$WT" c-codex ""
quarantine_id="$LIFECYCLE_ID"
lifecycle_update quarantined sess-live "$$" 0 "writer uncertain"
lifecycle_quarantine "writer uncertain"
check "quarantine retains live lock" "1" \
  "$([[ -d "$LIFECYCLE_LOCK_DIR" ]] && echo 1 || echo 0)"
( source "$LIB"; lifecycle_recover "$WT" "$quarantine_id" ) >/dev/null 2>&1
check "recovery refuses a live process group" "1" "$?"

bash -c "source '$LIB'; lifecycle_recover '$WT' '$quarantine_id' --force" >/dev/null 2>&1
check "recovery has no force option" "2" "$?"

# Test cleanup only: the production recovery path correctly refused this live owner.
rm -rf "$LIFECYCLE_LOCK_DIR"
rm -f "$LIFECYCLE_STATE_FILE"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
