#!/usr/bin/env bash
# Assert Claude and Hermes manifest versions stay synchronized.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PASS=0
FAIL=0
check() {
  if [[ "$2" == "$3" ]]; then
    echo "ok   - $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL - $1 (expected [$2], got [$3])"
    FAIL=$((FAIL + 1))
  fi
}

pick_python() {
  local candidate
  for candidate in "${PYTHON:-}" "$(command -v python3)" "$HOME/.pyenv/shims/python3"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if "$candidate" -c 'import yaml' 2>/dev/null; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

run_bump() {
  if [[ -n "$PY" ]]; then
    make -C "$ROOT" --no-print-directory PYTHON="$PY" "$@"
  else
    make -C "$ROOT" --no-print-directory "$@"
  fi
}

json_version="$(jq -r '.version' "$ROOT/.claude-plugin/plugin.json")"
PY="$(pick_python)" || PY=""
if [[ -n "$PY" ]]; then
  yaml_version="$("$PY" -c 'import sys, yaml; from pathlib import Path; print(yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))["version"])' "$ROOT/plugin.yaml")"
else
  yaml_version="$(awk '/^version:/ {print $2; exit}' "$ROOT/plugin.yaml")"
fi

check "plugin.json and plugin.yaml versions match" "$json_version" "$yaml_version"

if make -C "$ROOT" --no-print-directory check-bash5 BASH5=/bin/true \
    >/dev/null 2>&1; then
  arbitrary_bash_status="accepted"
else
  arbitrary_bash_status="rejected"
fi
check "test gate rejects arbitrary successful executable" "rejected" "$arbitrary_bash_status"

if grep -Fq 'mktemp "$$(dirname "$(PLUGIN_JSON)")/' "$ROOT/Makefile" &&
    grep -Fq 'mktemp "$$(dirname "$(HERMES_PLUGIN_YAML)")/' "$ROOT/Makefile"; then
  replacement_temp_location="same-directory"
else
  replacement_temp_location="other"
fi
check "replacement manifests use target directories" "same-directory" "$replacement_temp_location"

if [[ -n "$PY" ]]; then
  invalid_yaml="$(mktemp)"
  printf 'version: [\n' > "$invalid_yaml"
  if "$PY" -c 'import sys, yaml; from pathlib import Path; yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))' "$invalid_yaml" >/dev/null 2>&1; then
    check "invalid yaml candidate is rejected" "fail" "pass"
  else
    check "invalid yaml candidate is rejected" "fail" "fail"
  fi
  rm -f "$invalid_yaml"

  invalid_json="$(mktemp)"
  printf '{invalid' > "$invalid_json"
  if jq -e . "$invalid_json" >/dev/null 2>&1; then
    check "invalid json candidate is rejected" "fail" "pass"
  else
    check "invalid json candidate is rejected" "fail" "fail"
  fi
  rm -f "$invalid_json"
fi

fixture_root="$(mktemp -d)"
trap 'rm -rf "$fixture_root"' EXIT

rollback_fixture="$fixture_root/rollback"
mkdir -p "$rollback_fixture"
cp "$ROOT/.claude-plugin/plugin.json" "$rollback_fixture/plugin.json"
cp "$ROOT/plugin.yaml" "$rollback_fixture/plugin.yaml"
json_before="$(cat "$rollback_fixture/plugin.json")"
yaml_before="$(cat "$rollback_fixture/plugin.yaml")"
if CSC_BUMP_FAIL_AFTER_JSON=1 run_bump _bump PART=patch \
  PLUGIN_JSON="$rollback_fixture/plugin.json" \
  HERMES_PLUGIN_YAML="$rollback_fixture/plugin.yaml" >/dev/null 2>&1; then
  rollback_status="passed"
else
  rollback_status="failed"
fi
check "forced json replacement failure aborts bump" "failed" "$rollback_status"
check "json replacement rollback restores plugin.json bytes" "$json_before" "$(cat "$rollback_fixture/plugin.json")"
check "json replacement rollback restores plugin.yaml bytes" "$yaml_before" "$(cat "$rollback_fixture/plugin.yaml")"

backup_failure_fixture="$fixture_root/backup-failure"
mkdir -p "$backup_failure_fixture"
cp "$ROOT/.claude-plugin/plugin.json" "$backup_failure_fixture/plugin.json"
cp "$ROOT/plugin.yaml" "$backup_failure_fixture/plugin.yaml"
backup_json_before="$(cat "$backup_failure_fixture/plugin.json")"
backup_yaml_before="$(cat "$backup_failure_fixture/plugin.yaml")"
if CSC_BUMP_FAIL_AFTER_JSON_BACKUP=1 run_bump _bump PART=patch \
  PLUGIN_JSON="$backup_failure_fixture/plugin.json" \
  HERMES_PLUGIN_YAML="$backup_failure_fixture/plugin.yaml" >/dev/null 2>&1; then
  backup_failure_status="passed"
else
  backup_failure_status="failed"
fi
check "failure after first backup aborts bump" "failed" "$backup_failure_status"
check "first-backup failure preserves plugin.json bytes" "$backup_json_before" "$(cat "$backup_failure_fixture/plugin.json")"
check "first-backup failure preserves plugin.yaml bytes" "$backup_yaml_before" "$(cat "$backup_failure_fixture/plugin.yaml")"

output_failure_fixture="$fixture_root/output-failure"
mkdir -p "$output_failure_fixture"
cp "$ROOT/.claude-plugin/plugin.json" "$output_failure_fixture/plugin.json"
cp "$ROOT/plugin.yaml" "$output_failure_fixture/plugin.yaml"
output_json_before="$(cat "$output_failure_fixture/plugin.json")"
output_yaml_before="$(cat "$output_failure_fixture/plugin.yaml")"
if CSC_BUMP_FAIL_AFTER_OUTPUT=1 run_bump _bump PART=patch \
  PLUGIN_JSON="$output_failure_fixture/plugin.json" \
  HERMES_PLUGIN_YAML="$output_failure_fixture/plugin.yaml" >/dev/null 2>&1; then
  output_failure_status="passed"
else
  output_failure_status="failed"
fi
check "failure after final output aborts bump" "failed" "$output_failure_status"
check "final-output failure restores plugin.json bytes" "$output_json_before" "$(cat "$output_failure_fixture/plugin.json")"
check "final-output failure restores plugin.yaml bytes" "$output_yaml_before" "$(cat "$output_failure_fixture/plugin.yaml")"

drift_fixture="$fixture_root/drift"
mkdir -p "$drift_fixture"
cp "$ROOT/.claude-plugin/plugin.json" "$drift_fixture/plugin.json"
cp "$ROOT/plugin.yaml" "$drift_fixture/plugin.yaml"
awk '/^version:/ {print "version: 9.9.9"; next} {print}' \
  "$drift_fixture/plugin.yaml" > "$drift_fixture/plugin.yaml.tmp"
mv "$drift_fixture/plugin.yaml.tmp" "$drift_fixture/plugin.yaml"
drift_json_before="$(cat "$drift_fixture/plugin.json")"
drift_yaml_before="$(cat "$drift_fixture/plugin.yaml")"
if run_bump _bump PART=patch \
  PLUGIN_JSON="$drift_fixture/plugin.json" \
  HERMES_PLUGIN_YAML="$drift_fixture/plugin.yaml" >/dev/null 2>&1; then
  drift_status="passed"
else
  drift_status="failed"
fi
check "pre-existing drift aborts bump" "failed" "$drift_status"
check "drift abort leaves plugin.json bytes unchanged" "$drift_json_before" "$(cat "$drift_fixture/plugin.json")"
check "drift abort leaves plugin.yaml bytes unchanged" "$drift_yaml_before" "$(cat "$drift_fixture/plugin.yaml")"

success_fixture="$fixture_root/success"
mkdir -p "$success_fixture"
jq '.version = "1.0.0"' "$ROOT/.claude-plugin/plugin.json" > "$success_fixture/plugin.json"
awk '/^version:/ {print "version: 1.0.0"; next} {print}' "$ROOT/plugin.yaml" > "$success_fixture/plugin.yaml"
if run_bump _bump PART=patch \
  PLUGIN_JSON="$success_fixture/plugin.json" \
  HERMES_PLUGIN_YAML="$success_fixture/plugin.yaml" >/dev/null 2>&1; then
  success_status="passed"
else
  success_status="failed"
fi
success_json="$(jq -r '.version' "$success_fixture/plugin.json")"
success_yaml="$(awk '/^version:/ {print $2; exit}' "$success_fixture/plugin.yaml")"
check "fixture bump succeeds" "passed" "$success_status"
check "fixture bump advances plugin.json version" "1.0.1" "$success_json"
check "fixture bump advances plugin.yaml version" "1.0.1" "$success_yaml"

echo "---"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
