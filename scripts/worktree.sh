#!/usr/bin/env bash
# worktree.sh — create and remove the coder's isolated worktree.
#   worktree.sh prepare   -> creates <feature>-work and a sibling worktree folder
#   worktree.sh remove    -> tears it down, refusing if work would be lost
# Only one line of JSON reaches stdout; diagnostics go to stderr.
set -uo pipefail

die() { echo "error: $*" >&2; exit 1; }

# Folders brought into the worktree, when present and ignored by git.
DEP_LIST=(node_modules .venv venv vendor .bundle target .gradle .m2 .tox .cargo)
# Never brought across, even if a name above were to match one of these.
DENY_GLOBS=('.env*' '*.pem' '*.key' '*.p12' 'credentials*' '.netrc' '.npmrc' '.aws' '.ssh')
# Above this many entries, a folder is skipped unless cloning is available.
BIG_DIR_ENTRIES=5000

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"
ROOT="$(cd "$ROOT" && pwd -P)"
FEATURE="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
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

TMP_ERR="$(mktemp)"
trap 'rm -f "$TMP_ERR"' EXIT

branch_exists() { git -C "$ROOT" rev-parse --verify --quiet "refs/heads/$1" >/dev/null; }

# Is $WT registered to this repository and checked out on $WORK?
wt_registered() {
  git -C "$ROOT" worktree list --porcelain \
    | awk -v p="$WT" -v b="refs/heads/$WORK" '
        /^worktree /   { cur = substr($0, 10) }
        /^branch /     { if (cur == p && substr($0, 8) == b) { found = 1 } }
        END            { exit found ? 0 : 1 }'
}

# Path of this worktree's private git directory, where the manifest lives.
wt_git_dir() { git -C "$WT" rev-parse --absolute-git-dir 2>/dev/null; }

# Probe cloning between the ACTUAL source and destination, not in /tmp. A clone
# only works within one filesystem, so a /tmp probe can say yes while the real
# copy silently falls back to a full copy and skips the size guard.
clone_supported() {
  local a="$ROOT/.csc-clone-probe.$$" b="$WT/.csc-clone-probe.$$" rc=1
  mkdir -p "$a" && : > "$a/f" || { rm -rf "$a"; return 1; }
  if cp -Rc "$a" "$b" 2>/dev/null || cp -R --reflink=always "$a" "$b" 2>/dev/null; then
    rc=0
  fi
  rm -rf "$a" "$b"
  return $rc
}

# CLONE_USED is set by clone_dir to 1 when a real clone happened, 0 when it fell
# back to a full copy. The caller needs to know which it got.
CLONE_USED=0

# clone_dir <src> <dst> — copy-on-write where possible, plain copy otherwise.
# NEVER a symbolic link: a link is a two-way door back into the main checkout.
clone_dir() {
  CLONE_USED=1
  cp -Rc "$1" "$2" 2>/dev/null && return 0
  cp -R --reflink=always "$1" "$2" 2>/dev/null && return 0
  CLONE_USED=0
  cp -R "$1" "$2" 2>/dev/null && return 0
  return 1
}

# resolve_link <path-to-symlink> — echoes the absolute path its target names.
# The target need not exist. Runs in a subshell so the cd cannot leak out.
resolve_link() (
  local l="$1" tgt d b
  tgt="$(readlink "$l")"
  if [[ "$tgt" = /* ]]; then d="$(dirname "$tgt")"; else d="$(dirname "$l")/$(dirname "$tgt")"; fi
  b="$(basename "$tgt")"
  if cd "$d" 2>/dev/null; then echo "$(pwd -P)/$b"; else echo "$d/$b"; fi
)

# neutralize_symlinks <dir> — delete every symlink under <dir> whose target
# resolves OUTSIDE the worktree. Echoes how many it removed.
#
# This is the hole three drafts of the design kept leaving open. `cp -R` and
# `cp -Rc` both PRESERVE symlinks rather than following them, so an absolute
# link inside node_modules (common in monorepos and in some package managers)
# still points at the main checkout after copying. Writing through it in the
# worktree overwrites the real file. Verified 2026-08-22: doing exactly that
# changed a file in the main checkout.
#
# Links that resolve back inside the copied directory are kept: node_modules/.bin
# entries normally point within node_modules and are needed for tests to run.
neutralize_symlinks() {
  local dir="$1" removed=0 l abs real
  real="$(cd "$1" 2>/dev/null && pwd -P)" || return 0
  case "$real/" in
    "$WT"/*) : ;;
    *) echo "refusing to operate outside the worktree: $real" >&2; return 0 ;;
  esac
  dir="$real"
  while IFS= read -r l; do
    [[ -n "$l" ]] || continue
    abs="$(resolve_link "$l")"
    case "$abs/" in
      "$dir"/*) : ;;                     # stays inside the copied tree: safe
      *) rm -f "$l"; removed=$((removed+1)) ;;
    esac
  done < <(find "$dir" -type l 2>/dev/null)
  echo "$removed"
}

# purge_denied <dir> — delete anything inside <dir> whose BASENAME matches a deny
# pattern. Echoes how many it removed.
#
# DENY_GLOBS used to be checked only against the top-level names in DEP_LIST, none of
# which can match a pattern like `.env*` — so it was dead code and nothing filtered the
# CONTENTS of a copied tree. Verified before this fix: node_modules/some-pkg/.env and
# id_rsa.pem were both copied into the worktree and exposed to the external tool.
purge_denied() {
  local dir="$1" removed=0 g victim real
  real="$(cd "$1" 2>/dev/null && pwd -P)" || return 0
  case "$real/" in
    "$WT"/*) : ;;
    *) echo "refusing to operate outside the worktree: $real" >&2; return 0 ;;
  esac
  dir="$real"
  for g in "${DENY_GLOBS[@]}"; do
    while IFS= read -r victim; do
      [[ -n "$victim" ]] || continue
      rm -rf "$victim"
      removed=$((removed+1))
    done < <(find "$dir" -name "$g" -print 2>/dev/null)
  done
  echo "$removed"
}

denied() {
  # Top-level guard only; nested secret filtering is done by purge_denied.
  local name="$1" g
  for g in "${DENY_GLOBS[@]}"; do
    # shellcheck disable=SC2053
    [[ "$name" == $g ]] && return 0
  done
  return 1
}

cmd_prepare() {
  [[ "$FEATURE" != "main" && "$FEATURE" != "master" ]] \
    || die "refusing to run on '$FEATURE'. Create a feature branch first."
  [[ "$FEATURE" != *-work ]] \
    || die "refusing to run on '$FEATURE': it is already a work branch."

  local have_branch=0 have_wt=0 gd origin_file origin_feature
  branch_exists "$WORK" && have_branch=1
  [[ -e "$WT" ]] && have_wt=1

  if [[ $have_branch -eq 1 && $have_wt -eq 1 ]]; then
    wt_registered || die "'$WT' exists but is not this repository's worktree for '$WORK'. Inspect it by hand."
    if [[ -n "$RUN_ID" ]]; then
      gd="$(wt_git_dir)" || die "cannot inspect run-scoped worktree '$WT'."
      origin_file="$gd/csc-origin-feature"
      [[ -r "$origin_file" ]] \
        || die "cannot verify the original feature for run-scoped worktree '$WT'. Inspect it by hand."
      IFS= read -r origin_feature < "$origin_file" \
        || die "cannot read the original feature for run-scoped worktree '$WT'. Inspect it by hand."
      [[ "$origin_feature" == "$FEATURE" ]] \
        || die "run-scoped worktree '$WT' belongs to feature '$origin_feature', not '$FEATURE'."
    fi
    [[ -z "$(git -C "$WT" status --porcelain 2>/dev/null)" ]] \
      || die "worktree '$WT' has uncommitted changes from an earlier run. Commit, discard, or remove it."
    # Strict on purpose: a clean branch merely DESCENDED from the feature branch
    # may carry commits from an abandoned run, which this run's first
    # fast-forward would silently adopt as its own.
    if [[ "$(git -C "$ROOT" rev-parse "$WORK")" != "$(git -C "$ROOT" rev-parse "$FEATURE")" ]]; then
      echo "error: '$WORK' is not at the same commit as '$FEATURE'. Extra commits:" >&2
      git -C "$ROOT" log --oneline "$FEATURE..$WORK" >&2
      echo "Merge them deliberately or delete the branch, then run again." >&2
      exit 1
    fi
  elif [[ $have_branch -eq 1 || $have_wt -eq 1 ]]; then
    [[ $have_branch -eq 1 ]] \
      && die "branch '$WORK' exists but its worktree does not. Delete the branch or restore the worktree."
    die "'$WT' already exists but is not a worktree for '$WORK'. Move it aside."
  else
    git -C "$ROOT" worktree add -q -b "$WORK" "$WT" "$FEATURE" \
      || die "git worktree add failed"
    if [[ -n "$RUN_ID" ]]; then
      gd="$(wt_git_dir)" || die "cannot inspect new run-scoped worktree '$WT'."
      printf '%s\n' "$FEATURE" > "$gd/csc-origin-feature" \
        || die "cannot record the original feature for run-scoped worktree '$WT'."
    fi
  fi

  local manifest
  gd="$(wt_git_dir)"
  manifest="$gd/csc-copied"

  # Carry forward what an earlier prepare copied. Rewriting this from an empty
  # list on a reuse would lose the record, and `remove` would then leave those
  # folders behind and fail on the untracked files it did not know about.
  local copied=() skipped=() neutralized=0 purged=0 d entries removed
  if [[ -r "$manifest" ]]; then
    while IFS= read -r d; do [[ -n "$d" ]] && copied+=("$d"); done < "$manifest"
  fi

  local can_clone=0
  clone_supported && can_clone=1

  for d in "${DEP_LIST[@]}"; do
    [[ -e "$ROOT/$d" ]] || continue
    denied "$d" && continue
    git -C "$ROOT" check-ignore -q "$d" || continue
    # A symlinked dependency folder cannot be safely copied: `cp -R` copies the LINK,
    # which would put a door back into the user's files inside the worktree, `find`
    # does not descend into it so purge_denied would silently skip the tree, and
    # resolving through it makes neutralize_symlinks delete files outside the
    # worktree. Verified: writing in the worktree changed the user's real file.
    # Skipping is reported to the user, who can decide what to do.
    if [[ -L "$ROOT/$d" ]]; then
      skipped+=("$d")
      continue
    fi
    [[ -e "$WT/$d" ]] && continue

    if [[ $can_clone -eq 0 ]]; then
      entries=$(find "$ROOT/$d" | head -n $((BIG_DIR_ENTRIES + 1)) | wc -l | tr -d ' ')
      if [[ "$entries" -gt "$BIG_DIR_ENTRIES" ]]; then
        skipped+=("$d"); continue
      fi
    fi

    if clone_dir "$ROOT/$d" "$WT/$d"; then
      removed="$(neutralize_symlinks "$WT/$d")"
      neutralized=$(( neutralized + removed ))
      purged=$(( purged + $(purge_denied "$WT/$d") ))
      copied+=("$d")
    else
      rm -rf "${WT:?}/$d"
      skipped+=("$d")
    fi
  done

  # remove runs later as a separate invocation, so persist what we created.
  if [[ -n "$gd" ]]; then
    : > "$manifest"
    local c
    for c in "${copied[@]:-}"; do [[ -n "$c" ]] && echo "$c" >> "$manifest"; done
  fi

  jq -nc --arg wt "$WT" --arg work "$WORK" --arg feature "$FEATURE" \
         --arg copied "$(IFS=,; echo "${copied[*]:-}")" \
         --arg skipped "$(IFS=,; echo "${skipped[*]:-}")" \
         --argjson neutralized "$neutralized" --argjson purged "$purged" \
         --argjson cloned "$can_clone" \
    '{status:"READY", worktree:$wt, work_branch:$work, feature_branch:$feature,
      copied:($copied|split(",")|map(select(length>0))),
      skipped:($skipped|split(",")|map(select(length>0))),
      neutralized_symlinks:$neutralized, purged_secrets:$purged,
      clone_supported:($cloned == 1)}'
}

cmd_remove() {
  branch_exists "$WORK" || {
    jq -nc --arg work "$WORK" \
      '{status:"REMOVED", work_branch:$work, unmerged:[], diagnostic:"nothing to remove"}'
    return 0
  }

  # Everything below uses `branch -d`, not `-D`. It is safe precisely because
  # this ancestry check has already proved nothing would be lost.
  if ! git -C "$ROOT" merge-base --is-ancestor "$WORK" "$FEATURE"; then
    local unmerged
    unmerged="$(git -C "$ROOT" log --oneline "$FEATURE..$WORK" | jq -R . | jq -sc .)"
    jq -nc --arg work "$WORK" --argjson unmerged "${unmerged:-[]}" \
      '{status:"REFUSED", work_branch:$work, unmerged:$unmerged,
        diagnostic:"work branch has commits not on the feature branch; nothing was deleted"}'
    return 1
  fi

  # Delete ONLY what prepare created. git worktree remove refuses to run while
  # untracked files are present, and the copied dependencies are untracked.
  local gd manifest d
  gd="$(wt_git_dir)"
  manifest="$gd/csc-copied"
  if [[ -r "$manifest" ]]; then
    # Remove each listed dependency folder whole, not only the files prepare copied —
    # acceptable because these are disposable caches.
    while IFS= read -r d; do
      [[ -n "$d" ]] || continue
      # The manifest lives under the worktree's git dir, which the external coder can
      # write to. Never feed an unvalidated name to rm -rf: verified that "../VICTIM"
      # deleted a file outside the worktree. Accept only a plain name that is also a
      # known dependency folder.
      [[ "$d" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ignoring suspicious manifest entry: $d" >&2; continue; }
      local known=0 k
      for k in "${DEP_LIST[@]}"; do [[ "$k" == "$d" ]] && known=1 && break; done
      [[ "$known" -eq 1 ]] || { echo "ignoring unknown manifest entry: $d" >&2; continue; }
      rm -rf "${WT:?}/$d"
    done < "$manifest"
  fi

  # Deliberately NOT --force, and deliberately no `rm -rf` fallback. Both would
  # delete files the user created in the worktree that this script never made.
  # If anything unexpected is still there, stop and say so; the user can look.
  if ! git -C "$ROOT" worktree remove "$WT" 2>"$TMP_ERR"; then
    local leftover
    leftover="$(git -C "$WT" status --porcelain 2>/dev/null | head -20)"
    jq -nc --arg work "$WORK" --arg diag "$(cat "$TMP_ERR" 2>/dev/null)" \
           --arg leftover "$leftover" \
      '{status:"REFUSED", work_branch:$work, unmerged:[],
        diagnostic:("could not remove the worktree; nothing else was deleted: "
                    + $diag + (if $leftover == "" then "" else "\nremaining: " + $leftover end))}'
    return 1
  fi
  rm -f "$manifest"

  if ! git -C "$ROOT" branch -d "$WORK" >/dev/null 2>"$TMP_ERR"; then
    jq -nc --arg work "$WORK" --arg diag "$(cat "$TMP_ERR" 2>/dev/null)" \
      '{status:"REFUSED", work_branch:$work, unmerged:[],
        diagnostic:("worktree removed but the branch could not be deleted: " + $diag)}'
    return 1
  fi

  git -C "$ROOT" worktree prune

  jq -nc --arg work "$WORK" \
    '{status:"REMOVED", work_branch:$work, unmerged:[], diagnostic:""}'
}

case "${1:-}" in
  prepare) cmd_prepare ;;
  remove)  cmd_remove ;;
  *) echo "usage: worktree.sh prepare|remove" >&2; exit 2 ;;
esac
