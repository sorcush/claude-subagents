#!/usr/bin/env bash
# coder-lifecycle.sh — exclusive ownership and recovery state for coder worktrees.
# Source this; do not execute it.

LIFECYCLE_ID=""
LIFECYCLE_WORKTREE=""
LIFECYCLE_GIT_DIR=""
LIFECYCLE_LOCK_DIR=""
LIFECYCLE_STATE_FILE=""
LIFECYCLE_RECOVERY_LOCK=""
LIFECYCLE_DIAGNOSTIC=""
LIFECYCLE_CODER=""
LIFECYCLE_SESSION_ID=""
LIFECYCLE_PROCESS_GROUP_ID=""
LIFECYCLE_WRITER_STOPPED=1
LIFECYCLE_STARTED_AT=""
LIFECYCLE_OWNER_STARTED=""
LIFECYCLE_START_BRANCH=""
LIFECYCLE_START_COMMIT=""
LIFECYCLE_ATTEMPTS=0

lifecycle_resolve_paths() {  # <cwd>
  local cwd="$1" git_dir common_dir branch
  LIFECYCLE_DIAGNOSTIC=""
  [[ -d "$cwd" ]] || { LIFECYCLE_DIAGNOSTIC="worktree does not exist: $cwd"; return 1; }
  LIFECYCLE_WORKTREE="$(cd "$cwd" && pwd -P)" \
    || { LIFECYCLE_DIAGNOSTIC="cannot resolve worktree: $cwd"; return 1; }
  git -C "$LIFECYCLE_WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || { LIFECYCLE_DIAGNOSTIC="not a git worktree: $LIFECYCLE_WORKTREE"; return 1; }
  git_dir="$(git -C "$LIFECYCLE_WORKTREE" rev-parse --absolute-git-dir 2>/dev/null)" \
    || { LIFECYCLE_DIAGNOSTIC="cannot resolve private git directory"; return 1; }
  common_dir="$(git -C "$LIFECYCLE_WORKTREE" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || { LIFECYCLE_DIAGNOSTIC="cannot resolve common git directory"; return 1; }
  [[ "$git_dir" != "$common_dir" ]] \
    || { LIFECYCLE_DIAGNOSTIC="coder worktree must be a linked worktree"; return 1; }
  branch="$(git -C "$LIFECYCLE_WORKTREE" branch --show-current 2>/dev/null)"
  [[ -n "$branch" ]] || { LIFECYCLE_DIAGNOSTIC="coder worktree is detached"; return 1; }
  [[ "$branch" != main && "$branch" != master ]] \
    || { LIFECYCLE_DIAGNOSTIC="coder worktree cannot use $branch"; return 1; }

  LIFECYCLE_GIT_DIR="$git_dir"
  LIFECYCLE_LOCK_DIR="$git_dir/claude-subagents-coder.lock"
  LIFECYCLE_STATE_FILE="$git_dir/claude-subagents-coder-state.json"
  LIFECYCLE_RECOVERY_LOCK="$git_dir/claude-subagents-coder-recovery.lock"
}

lifecycle_changed_paths() {
  {
    git -C "$LIFECYCLE_WORKTREE" diff --name-only -z
    git -C "$LIFECYCLE_WORKTREE" diff --cached --name-only -z
    git -C "$LIFECYCLE_WORKTREE" ls-files --others --exclude-standard -z
  } | jq -Rs 'split("\u0000") | map(select(length > 0)) | unique'
}

lifecycle_process_started() {  # <pid>
  ps -p "$1" -o lstart= 2>/dev/null | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

lifecycle_pid_matches() {  # <pid> <recorded-start>
  local pid="$1" recorded="$2" observed
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -n "$recorded" ]] || return 0
  observed="$(lifecycle_process_started "$pid")"
  [[ -n "$observed" && "$observed" == "$recorded" ]]
}

lifecycle_group_alive() {  # <pgid>
  local pgid="$1"
  [[ "$pgid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 -"$pgid" 2>/dev/null
}

lifecycle_write_state() {  # <state> <session> <pgid> <writer-stopped> <diagnostic>
  local state="$1" session="$2" pgid="$3" stopped="$4" diagnostic="$5"
  local observed_branch observed_commit files tmp
  observed_branch="$(git -C "$LIFECYCLE_WORKTREE" branch --show-current 2>/dev/null)"
  observed_commit="$(git -C "$LIFECYCLE_WORKTREE" rev-parse HEAD 2>/dev/null)"
  files="$(lifecycle_changed_paths)" || return 1
  tmp="$LIFECYCLE_STATE_FILE.tmp.$BASHPID.$RANDOM"
  jq -n \
    --arg id "$LIFECYCLE_ID" \
    --argjson owner_pid "$BASHPID" \
    --arg owner_started "$LIFECYCLE_OWNER_STARTED" \
    --arg started_at "$LIFECYCLE_STARTED_AT" \
    --arg state "$state" \
    --arg coder "$LIFECYCLE_CODER" \
    --arg session "$session" \
    --arg pgid "$pgid" \
    --arg worktree "$LIFECYCLE_WORKTREE" \
    --arg branch "$observed_branch" \
    --arg start_branch "$LIFECYCLE_START_BRANCH" \
    --arg start_commit "$LIFECYCLE_START_COMMIT" \
    --arg observed_commit "$observed_commit" \
    --argjson attempts "$LIFECYCLE_ATTEMPTS" \
    --argjson stopped "$stopped" \
    --argjson files "$files" \
    --arg diagnostic "$diagnostic" \
    '{version:1,lifecycle_id:$id,state:$state,owner_pid:$owner_pid,
      owner_started:$owner_started,started_at:$started_at,coder:$coder,
      session_id:$session,process_group_id:$pgid,worktree:$worktree,
      branch:$branch,start_branch:$start_branch,start_commit:$start_commit,
      observed_commit:$observed_commit,attempts:$attempts,writer_stopped:$stopped,
      files_changed:$files,diagnostic:$diagnostic}' > "$tmp" \
    || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$LIFECYCLE_STATE_FILE"
  LIFECYCLE_SESSION_ID="$session"
  LIFECYCLE_PROCESS_GROUP_ID="$pgid"
  LIFECYCLE_WRITER_STOPPED="$stopped"
}

lifecycle_update() {
  lifecycle_write_state "$@"
}

lifecycle_release_lock() {
  local owner_file="$LIFECYCLE_LOCK_DIR/owner.json" owner_id=""
  [[ -d "$LIFECYCLE_LOCK_DIR" ]] || return 0
  [[ -r "$owner_file" ]] && owner_id="$(jq -r '.lifecycle_id // ""' "$owner_file" 2>/dev/null)"
  [[ "$owner_id" == "$LIFECYCLE_ID" ]] || return 1
  rm -f "$owner_file"
  rmdir "$LIFECYCLE_LOCK_DIR"
}

lifecycle_open() {  # <cwd> <coder> <resume-lifecycle-id>
  local cwd="$1" coder="$2" resume_id="${3:-}" state_id state_coder state_name
  local recorded_worktree recorded_branch recorded_commit recorded_files current_files owner_tmp owner_pid
  lifecycle_resolve_paths "$cwd" || return 1
  [[ -n "$coder" ]] || { LIFECYCLE_DIAGNOSTIC="coder key is required"; return 2; }
  if [[ -n "$resume_id" && ! "$resume_id" =~ ^[0-9]+-[0-9]+-[0-9]+$ ]]; then
    LIFECYCLE_DIAGNOSTIC="invalid lifecycle id"
    return 2
  fi
  if ! mkdir "$LIFECYCLE_LOCK_DIR" 2>/dev/null; then
    LIFECYCLE_DIAGNOSTIC="coder worktree is already owned or quarantined"
    return 1
  fi

  LIFECYCLE_CODER="$coder"
  owner_pid="$BASHPID"
  LIFECYCLE_OWNER_STARTED="$(lifecycle_process_started "$owner_pid")"
  LIFECYCLE_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if [[ -f "$LIFECYCLE_STATE_FILE" ]]; then
    state_id="$(jq -r '.lifecycle_id // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    state_coder="$(jq -r '.coder // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    state_name="$(jq -r '.state // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    recorded_worktree="$(jq -r '.worktree // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    recorded_branch="$(jq -r '.branch // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    recorded_commit="$(jq -r '.observed_commit // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    recorded_files="$(jq -cS '.files_changed // []' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
    current_files="$(lifecycle_changed_paths | jq -cS .)"
    if [[ "$state_name" != recoverable || "$resume_id" != "$state_id" \
          || "$coder" != "$state_coder" || "$recorded_worktree" != "$LIFECYCLE_WORKTREE" \
          || "$recorded_branch" != "$(git -C "$LIFECYCLE_WORKTREE" branch --show-current)" \
          || "$recorded_commit" != "$(git -C "$LIFECYCLE_WORKTREE" rev-parse HEAD)" \
          || "$recorded_files" != "$current_files" ]]; then
      LIFECYCLE_DIAGNOSTIC="recovery state does not match this resume"
      rmdir "$LIFECYCLE_LOCK_DIR"
      return 1
    fi
    LIFECYCLE_ID="$state_id"
    LIFECYCLE_SESSION_ID="$(jq -r '.session_id // ""' "$LIFECYCLE_STATE_FILE")"
    LIFECYCLE_START_BRANCH="$(jq -r '.start_branch // .branch // ""' "$LIFECYCLE_STATE_FILE")"
    LIFECYCLE_START_COMMIT="$(jq -r '.start_commit // .observed_commit // ""' "$LIFECYCLE_STATE_FILE")"
    LIFECYCLE_ATTEMPTS="$(jq -r '.attempts // 0' "$LIFECYCLE_STATE_FILE")"
  else
    if [[ -n "$resume_id" ]]; then
      LIFECYCLE_DIAGNOSTIC="no recovery state exists for lifecycle $resume_id"
      rmdir "$LIFECYCLE_LOCK_DIR"
      return 1
    fi
    if [[ -n "$(git -C "$LIFECYCLE_WORKTREE" status --porcelain)" ]]; then
      LIFECYCLE_DIAGNOSTIC="fresh coder task requires a clean worktree"
      rmdir "$LIFECYCLE_LOCK_DIR"
      return 1
    fi
    LIFECYCLE_ID="$(date -u +%s)-$BASHPID-$RANDOM"
    LIFECYCLE_START_BRANCH="$(git -C "$LIFECYCLE_WORKTREE" branch --show-current)"
    LIFECYCLE_START_COMMIT="$(git -C "$LIFECYCLE_WORKTREE" rev-parse HEAD)"
    LIFECYCLE_SESSION_ID=""
    LIFECYCLE_ATTEMPTS=0
  fi

  owner_tmp="$LIFECYCLE_LOCK_DIR/owner.json.tmp"
  jq -n --arg id "$LIFECYCLE_ID" --argjson pid "$BASHPID" \
    --arg started "$LIFECYCLE_OWNER_STARTED" --arg worktree "$LIFECYCLE_WORKTREE" \
    '{lifecycle_id:$id,owner_pid:$pid,owner_started:$started,worktree:$worktree}' \
    > "$owner_tmp" || return 1
  mv "$owner_tmp" "$LIFECYCLE_LOCK_DIR/owner.json"
  lifecycle_write_state active "$LIFECYCLE_SESSION_ID" "" true ""
}

lifecycle_finish() {
  [[ -n "$LIFECYCLE_STATE_FILE" ]] && rm -f "$LIFECYCLE_STATE_FILE"
  lifecycle_release_lock
}

lifecycle_block_recoverable() {  # <diagnostic>
  lifecycle_write_state recoverable "$LIFECYCLE_SESSION_ID" \
    "$LIFECYCLE_PROCESS_GROUP_ID" true "$1" || return 1
  lifecycle_release_lock
}

lifecycle_quarantine() {  # <diagnostic>
  lifecycle_write_state quarantined "$LIFECYCLE_SESSION_ID" \
    "$LIFECYCLE_PROCESS_GROUP_ID" false "$1"
}

lifecycle_recover() {  # <cwd> <lifecycle-id>
  [[ $# -eq 2 ]] || return 2
  local cwd="$1" requested_id="$2"
  local state_id state_name owner_pid owner_started pgid recorded_worktree
  local recorded_branch recorded_commit recorded_files current_files tmp recovery_pid
  local start_branch start_commit integrity_violation=0 restored_files
  lifecycle_resolve_paths "$cwd" || return 1
  [[ "$requested_id" =~ ^[0-9]+-[0-9]+-[0-9]+$ ]] \
    || { LIFECYCLE_DIAGNOSTIC="invalid lifecycle id"; return 2; }
  if ! mkdir "$LIFECYCLE_RECOVERY_LOCK" 2>/dev/null; then
    LIFECYCLE_DIAGNOSTIC="recovery is already in progress"
    return 1
  fi

  if [[ ! -r "$LIFECYCLE_STATE_FILE" ]]; then
    LIFECYCLE_DIAGNOSTIC="no recovery state exists"
    rmdir "$LIFECYCLE_RECOVERY_LOCK"
    return 1
  fi

  state_id="$(jq -r '.lifecycle_id // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  state_name="$(jq -r '.state // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  owner_pid="$(jq -r '.owner_pid // 0' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  owner_started="$(jq -r '.owner_started // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  pgid="$(jq -r '.process_group_id // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  recorded_worktree="$(jq -r '.worktree // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  recorded_branch="$(jq -r '.branch // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  recorded_commit="$(jq -r '.observed_commit // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  start_branch="$(jq -r '.start_branch // .branch // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  start_commit="$(jq -r '.start_commit // .observed_commit // ""' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  recorded_files="$(jq -cS '.files_changed // []' "$LIFECYCLE_STATE_FILE" 2>/dev/null)"
  current_files="$(lifecycle_changed_paths | jq -cS .)"

  if [[ "$recorded_branch" != "$start_branch" || "$recorded_commit" != "$start_commit" ]]; then
    integrity_violation=1
  fi
  restored_files="$recorded_files"
  if [[ "$integrity_violation" -eq 1 ]] \
     && git -C "$LIFECYCLE_WORKTREE" cat-file -e "$start_commit^{commit}" 2>/dev/null \
     && git -C "$LIFECYCLE_WORKTREE" cat-file -e "$recorded_commit^{commit}" 2>/dev/null; then
    restored_files="$({
      printf '%s' "$recorded_files" | jq -r '.[]' | while IFS= read -r path; do printf '%s\0' "$path"; done
      git -C "$LIFECYCLE_WORKTREE" diff --name-only -z "$start_commit..$recorded_commit" 2>/dev/null
    } | jq -Rs 'split("\u0000") | map(select(length > 0)) | unique' | jq -cS .)"
  fi

  if [[ "$state_id" != "$requested_id" || "$state_name" != quarantined \
        || "$recorded_worktree" != "$LIFECYCLE_WORKTREE" ]]; then
    LIFECYCLE_DIAGNOSTIC="quarantine state does not match this recovery"
    rmdir "$LIFECYCLE_RECOVERY_LOCK"
    return 1
  fi
  if [[ "$integrity_violation" -eq 1 ]]; then
    if [[ "$start_branch" != "$(git -C "$LIFECYCLE_WORKTREE" branch --show-current)" \
          || "$start_commit" != "$(git -C "$LIFECYCLE_WORKTREE" rev-parse HEAD)" \
          || ( "$current_files" != "$recorded_files" && "$current_files" != "$restored_files" \
               && "$current_files" != "[]" ) ]]; then
      LIFECYCLE_DIAGNOSTIC="restore the recorded starting branch and commit before recovery"
      rmdir "$LIFECYCLE_RECOVERY_LOCK"
      return 1
    fi
  elif [[ "$recorded_branch" != "$(git -C "$LIFECYCLE_WORKTREE" branch --show-current)" \
          || "$recorded_commit" != "$(git -C "$LIFECYCLE_WORKTREE" rev-parse HEAD)" \
          || "$recorded_files" != "$current_files" ]]; then
    LIFECYCLE_DIAGNOSTIC="quarantine state does not match this recovery"
    rmdir "$LIFECYCLE_RECOVERY_LOCK"
    return 1
  fi
  if lifecycle_pid_matches "$owner_pid" "$owner_started" || lifecycle_group_alive "$pgid"; then
    LIFECYCLE_DIAGNOSTIC="recorded writer is still alive"
    rmdir "$LIFECYCLE_RECOVERY_LOCK"
    return 1
  fi

  LIFECYCLE_ID="$state_id"
  LIFECYCLE_CODER="$(jq -r '.coder // ""' "$LIFECYCLE_STATE_FILE")"
  LIFECYCLE_SESSION_ID="$(jq -r '.session_id // ""' "$LIFECYCLE_STATE_FILE")"
  LIFECYCLE_PROCESS_GROUP_ID="$pgid"
  LIFECYCLE_STARTED_AT="$(jq -r '.started_at // ""' "$LIFECYCLE_STATE_FILE")"
  recovery_pid="$BASHPID"
  LIFECYCLE_OWNER_STARTED="$(lifecycle_process_started "$recovery_pid")"
  LIFECYCLE_START_BRANCH="$(jq -r '.start_branch // .branch // ""' "$LIFECYCLE_STATE_FILE")"
  LIFECYCLE_START_COMMIT="$(jq -r '.start_commit // .observed_commit // ""' "$LIFECYCLE_STATE_FILE")"
  LIFECYCLE_ATTEMPTS="$(jq -r '.attempts // 0' "$LIFECYCLE_STATE_FILE")"
  tmp="$LIFECYCLE_STATE_FILE.tmp.$BASHPID.$RANDOM"
  jq --arg branch "$(git -C "$LIFECYCLE_WORKTREE" branch --show-current)" \
    --arg commit "$(git -C "$LIFECYCLE_WORKTREE" rev-parse HEAD)" \
    --argjson files "$current_files" \
    '.state="recoverable" | .writer_stopped=true | .process_group_id="" |
     .branch=$branch | .observed_commit=$commit | .files_changed=$files' \
    "$LIFECYCLE_STATE_FILE" > "$tmp" || {
      rm -f "$tmp"
      rmdir "$LIFECYCLE_RECOVERY_LOCK"
      return 1
    }
  mv "$tmp" "$LIFECYCLE_STATE_FILE"
  if [[ -d "$LIFECYCLE_LOCK_DIR" ]]; then
    rm -f "$LIFECYCLE_LOCK_DIR/owner.json" "$LIFECYCLE_LOCK_DIR/owner.json.tmp"
    rmdir "$LIFECYCLE_LOCK_DIR" || {
      LIFECYCLE_DIAGNOSTIC="stale live lock could not be removed"
      rmdir "$LIFECYCLE_RECOVERY_LOCK"
      return 1
    }
  fi
  rmdir "$LIFECYCLE_RECOVERY_LOCK"
  return 0
}
