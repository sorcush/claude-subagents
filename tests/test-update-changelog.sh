#!/usr/bin/env bash
# Unit tests for update-changelog.sh. Run: bash tests/test-update-changelog.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/update-changelog.sh"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

HEADER_TITLE="# Changelog"

# --- creates a fresh file with header when none exists ---
f=$(mktemp -u)
printf '## [1.0.0] - 2026-01-01\n\n### Added\n- first release\n' | \
  bash "$SCRIPT" --version 1.0.0 --file "$f"
check "creates file" "1" "$([ -f "$f" ] && echo 1 || echo 0)"
check "has standard header" "1" "$(grep -c -- "$HEADER_TITLE" "$f")"
check "has the new section" "1" "$(grep -c -- '## \[1.0.0\]' "$f")"
check "header appears before section" "1" "$([ "$(grep -n "$HEADER_TITLE" "$f" | head -1 | cut -d: -f1)" -lt "$(grep -n '## \[1.0.0\]' "$f" | head -1 | cut -d: -f1)" ] && echo 1 || echo 0)"
rm -f "$f"

# --- inserts a new version above older ones, below the header ---
f=$(mktemp)
cat > "$f" <<'EOF'
# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-01-01

### Added
- first release
EOF
printf '## [1.1.0] - 2026-02-01\n\n### Fixed\n- a bug\n' | bash "$SCRIPT" --version 1.1.0 --file "$f"
lines=$(grep -n '^## \[' "$f" | cut -d: -f1)
first=$(echo "$lines" | sed -n '1p')
second=$(echo "$lines" | sed -n '2p')
check "two version headings present" "2" "$(echo "$lines" | wc -l | tr -d ' ')"
check "new version (1.1.0) comes first" "1" "$(sed -n "${first}p" "$f" | grep -c '1\.1\.0')"
check "old version (1.0.0) comes second, untouched" "1" "$(sed -n "${second}p" "$f" | grep -c '1\.0\.0')"
check "old section body survived" "1" "$(grep -c 'first release' "$f")"
rm -f "$f"

# --- replace-in-place: re-running for the SAME version replaces, not duplicates ---
f=$(mktemp)
cat > "$f" <<'EOF'
# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-03-01

### Added
- stale draft, written before a retry
EOF
printf '## [2.0.0] - 2026-03-01\n\n### Added\n- final correct content\n### Fixed\n- also this\n' | \
  bash "$SCRIPT" --version 2.0.0 --file "$f"
check "still exactly one 2.0.0 heading" "1" "$(grep -c '^## \[2\.0\.0\]' "$f")"
check "stale content is gone" "0" "$(grep -c 'stale draft' "$f")"
check "fresh content present" "1" "$(grep -c 'final correct content' "$f")"
check "fresh Fixed section present" "1" "$(grep -c 'also this' "$f")"
rm -f "$f"

# --- replacing a version that ISN'T the most recent doesn't disturb neighbors ---
f=$(mktemp)
cat > "$f" <<'EOF'
# Changelog

## [3.0.0] - 2026-05-01

### Added
- newest

## [2.0.0] - 2026-04-01

### Added
- middle, stale

## [1.0.0] - 2026-03-01

### Added
- oldest
EOF
printf '## [2.0.0] - 2026-04-01\n\n### Added\n- middle, fixed\n' | bash "$SCRIPT" --version 2.0.0 --file "$f"
check "three headings still present" "3" "$(grep -c '^## \[' "$f")"
check "newest untouched" "1" "$(grep -c 'newest' "$f")"
check "middle replaced" "1" "$(grep -c 'middle, fixed' "$f")"
check "middle stale text gone" "0" "$(grep -c 'middle, stale' "$f")"
check "oldest untouched" "1" "$(grep -c 'oldest' "$f")"
heading_lines=$(grep -n '^## \[' "$f" | cut -d: -f1)
first_line=$(echo "$heading_lines" | sed -n '1p')
second_line=$(echo "$heading_lines" | sed -n '2p')
third_line=$(echo "$heading_lines" | sed -n '3p')
check "3.0.0 still first heading" "1" "$(sed -n "${first_line}p" "$f" | grep -c '3\.0\.0')"
check "2.0.0 still second heading" "1" "$(sed -n "${second_line}p" "$f" | grep -c '2\.0\.0')"
check "1.0.0 still third heading" "1" "$(sed -n "${third_line}p" "$f" | grep -c '1\.0\.0')"
rm -f "$f"

# --- idempotent: running twice in a row with identical input converges ---
f=$(mktemp -u)
section=$'## [4.0.0] - 2026-06-01\n\n### Added\n- idempotency check\n'
printf '%s' "$section" | bash "$SCRIPT" --version 4.0.0 --file "$f"
printf '%s' "$section" | bash "$SCRIPT" --version 4.0.0 --file "$f"
check "one heading after two identical runs" "1" "$(grep -c '^## \[4\.0\.0\]' "$f")"
rm -f "$f"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
