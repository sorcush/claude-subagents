#!/usr/bin/env bash
# Unit tests for scripts/lib/pool.sh and scripts/pool.sh. Run: bash tests/test-pool.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$HERE/../scripts/pool.sh"

PASS=0
FAIL=0
check() {  # check <description> <expected> <actual>
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"; PASS=$((PASS+1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"; FAIL=$((FAIL+1))
  fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

write() {  # write <path> <json>
  printf '%s\n' "$2" > "$TMP/$1"
}

# rc_of <fixture> -> exit code of `pool.sh list reviewers` against it
rc_of() {
  CSC_REVIEWERS_JSON="$TMP/$1" bash "$CLI" list reviewers >/dev/null 2>&1
  echo $?
}

GOOD='{"reviewers":[
  {"key":"a-one","label":"A One","harness":"codex","model":"m-1","default":true},
  {"key":"b-two","label":"B Two","harness":"cursor","model":"m.2"}]}'
write good.json "$GOOD"

out=$(CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" list reviewers 2>/dev/null)
check "valid pool exits 0"            "0" "$(rc_of good.json)"
check "role echoed"                   "reviewers" "$(echo "$out" | jq -r '.role')"
check "two entries"                   "2" "$(echo "$out" | jq '.entries|length')"
check "file order preserved"          "a-one b-two" "$(echo "$out" | jq -r '[.entries[].key]|join(" ")')"
check "default flag carried"          "true" "$(echo "$out" | jq -r '.entries[0].default')"
check "listing has no model field"    "0" "$(echo "$out" | jq '[.entries[]|has("model")]|map(select(.))|length')"
check "single JSON line on stdout"    "1" "$(echo "$out" | wc -l | tr -d ' ')"

# --- rejections: each must exit 2 ---
write not-json.json 'this is not json'
write wrong-key.json '{"coders":[{"key":"a","label":"A","harness":"codex","model":"m"}]}'
write not-array.json '{"reviewers":{}}'
write empty.json '{"reviewers":[]}'
write not-object.json '{"reviewers":["a"]}'
write extra-key.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m","lable":"typo"}]}'
write no-label.json '{"reviewers":[{"key":"a","harness":"codex","model":"m"}]}'
write bad-key.json '{"reviewers":[{"key":"Bad Key","label":"A","harness":"codex","model":"m"}]}'
write bad-model.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"bad model"}]}'
write bad-harness.json '{"reviewers":[{"key":"a","label":"A","harness":"Codex","model":"m"}]}'
write traversal.json '{"reviewers":[{"key":"a","label":"A","harness":"../../etc","model":"m"}]}'
write absent-harness.json '{"reviewers":[{"key":"a","label":"A","harness":"nosuchtool","model":"m"}]}'
write dup-key.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m"},{"key":"a","label":"B","harness":"codex","model":"m"}]}'
write two-defaults.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m","default":true},{"key":"b","label":"B","harness":"codex","model":"m","default":true}]}'
write bad-default.json '{"reviewers":[{"key":"a","label":"A","harness":"codex","model":"m","default":"yes"}]}'
write newline-label.json '{"reviewers":[{"key":"a","label":"A\nB","harness":"codex","model":"m"}]}'

for f in not-json wrong-key not-array empty not-object extra-key no-label bad-key \
         bad-model bad-harness traversal absent-harness dup-key two-defaults \
         bad-default newline-label; do
  check "rejects $f" "2" "$(rc_of "$f.json")"
done

check "missing file exits 2" "2" "$(rc_of does-not-exist.json)"

# --- lookup by key ---
out=$(CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" get reviewers b-two 2>/dev/null)
check "get returns the model"  "m.2"    "$(echo "$out" | jq -r '.model')"
check "get returns harness"    "cursor" "$(echo "$out" | jq -r '.harness')"

CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" get reviewers nope >/dev/null 2>&1
check "unknown key exits 2" "2" "$?"

err=$(CSC_REVIEWERS_JSON="$TMP/good.json" bash "$CLI" get reviewers nope 2>&1 >/dev/null)
check "unknown key lists valid keys" "1" "$([[ "$err" == *a-one* && "$err" == *b-two* ]] && echo 1 || echo 0)"

# --- the shipped pool files are themselves valid ---
check "shipped reviewers.json valid" "0" "$(bash "$CLI" list reviewers >/dev/null 2>&1; echo $?)"
check "shipped coders.json valid"    "0" "$(bash "$CLI" list coders   >/dev/null 2>&1; echo $?)"
check "shipped reviewers count"      "4" "$(bash "$CLI" list reviewers | jq '.entries|length')"
check "shipped coders count"         "2" "$(bash "$CLI" list coders   | jq '.entries|length')"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
