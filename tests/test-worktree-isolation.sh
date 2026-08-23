#!/usr/bin/env bash
# Proves success criterion 4: the coder cannot write into the main checkout.
# This is the guarantee two earlier drafts of the design got wrong by using
# symbolic links, so it is proved here rather than asserted in prose.
# Run: bash tests/test-worktree-isolation.sh
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

r="$ROOT/app"
mkdir -p "$r"
git -C "$r" init -q -b main
git -C "$r" config user.email t@example.com
git -C "$r" config user.name Test
printf 'node_modules/\n' > "$r/.gitignore"
echo hi > "$r/README.md"
git -C "$r" add -A && git -C "$r" commit -q -m init
git -C "$r" checkout -q -b feature/iso

mkdir -p "$r/node_modules/pkg" "$r/node_modules/.bin" "$r/secret"
echo "ORIGINAL" > "$r/node_modules/pkg/index.js"
echo "TOPSECRET" > "$r/secret/key.txt"

# Three symlink fixtures. Without these the test proves nothing: `cp -R` and
# `cp -Rc` both PRESERVE symlinks rather than following them, so a copied
# dependency tree can still contain a door back into the main checkout.
#   1. absolute link out of the repo   -> must be removed
#   2. relative link climbing out      -> must be removed
#   3. link staying inside the copy    -> must be KEPT, .bin entries need it
ln -s "$r/secret" "$r/node_modules/abs-escape"
ln -s ../../secret "$r/node_modules/pkg/rel-escape"
ln -s ../pkg/index.js "$r/node_modules/.bin/tool"

out=$(cd "$r" && bash "$SCRIPT" prepare 2>/dev/null)
wt=$(echo "$out" | jq -r '.worktree')

check "dependency arrived in the worktree" "ORIGINAL" "$(cat "$wt/node_modules/pkg/index.js")"
check "the folder itself is not a symlink" "1" "$([[ ! -L "$wt/node_modules" ]] && echo 1 || echo 0)"

# The escaping links must be gone.
check "absolute escaping link removed" "1" "$([[ ! -e "$wt/node_modules/abs-escape" ]] && echo 1 || echo 0)"
check "relative escaping link removed" "1" "$([[ ! -e "$wt/node_modules/pkg/rel-escape" ]] && echo 1 || echo 0)"
check "prepare counted what it removed" "2" "$(echo "$out" | jq -r '.neutralized_symlinks')"

# The internal link must survive: removing it would break every .bin entry and
# make the verify command fail for a reason that has nothing to do with the code.
check "internal link is kept" "1" "$([[ -L "$wt/node_modules/.bin/tool" ]] && echo 1 || echo 0)"

# Nothing left under the copy may resolve outside the worktree.
escapes=0
while IFS= read -r l; do
  [[ -n "$l" ]] || continue
  tgt="$(cd "$(dirname "$l")" && cd "$(dirname "$(readlink "$l")")" 2>/dev/null && pwd -P)" || continue
  case "$tgt/" in "$wt"/*) : ;; *) escapes=$((escapes+1)) ;; esac
done < <(find "$wt/node_modules" -type l 2>/dev/null)
check "no surviving link resolves outside the worktree" "0" "$escapes"

# Independent of symlink neutralisation: the copied tree must be a real copy, so
# writing through the internal .bin link that we deliberately KEPT must not reach the
# main checkout. If clone_dir ever regressed to linking rather than copying, this
# fails even though every escaping link was still removed correctly.
echo "MODIFIED BY CODER" > "$wt/node_modules/.bin/tool"
check "writing through a KEPT internal link does not reach main" "ORIGINAL" \
  "$(cat "$r/node_modules/pkg/index.js")"

# THE POINT: writing in the worktree must not reach the main checkout.
echo "MODIFIED BY CODER" > "$wt/node_modules/pkg/index.js"
check "main checkout file is untouched" "ORIGINAL" "$(cat "$r/node_modules/pkg/index.js")"
check "worktree file did change"        "MODIFIED BY CODER" "$(cat "$wt/node_modules/pkg/index.js")"

# Adding and deleting in the worktree must not reach the main checkout either.
echo new > "$wt/node_modules/pkg/added.js"
check "added file does not appear in main" "1" \
  "$([[ ! -e "$r/node_modules/pkg/added.js" ]] && echo 1 || echo 0)"
rm -f "$wt/node_modules/pkg/index.js"
check "deleting in the worktree does not delete in main" "1" \
  "$([[ -f "$r/node_modules/pkg/index.js" ]] && echo 1 || echo 0)"

# A symlinked dependency folder must be skipped, not copied. Copying it would place a
# symlink in the worktree pointing at the user's real files. Verified before this fix:
# a write inside the worktree changed the user's real file.
r2="$ROOT/app2"; mkdir -p "$r2" "$ROOT/shared/pkg"
echo "REAL" > "$ROOT/shared/pkg/index.js"
git -C "$r2" init -q -b main
git -C "$r2" config user.email t@example.com; git -C "$r2" config user.name Test
printf 'node_modules\n' > "$r2/.gitignore"
echo hi > "$r2/README.md"
git -C "$r2" add -A && git -C "$r2" commit -q -m init
git -C "$r2" checkout -q -b feature/sym
ln -s "$ROOT/shared" "$r2/node_modules"
out2=$(cd "$r2" && bash "$SCRIPT" prepare 2>/dev/null)
wt2=$(echo "$out2" | jq -r '.worktree')
check "symlinked dep is skipped, not copied" "1" \
  "$(echo "$out2" | jq -r '.skipped|index("node_modules")|if . == null then 0 else 1 end')"
check "no symlinked dep lands in the worktree" "1" \
  "$([[ ! -e "$wt2/node_modules" ]] && echo 1 || echo 0)"
echo "TRY" > "$wt2/node_modules/pkg/index.js" 2>/dev/null || true
check "user's real file is untouched" "REAL" "$(cat "$ROOT/shared/pkg/index.js")"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
