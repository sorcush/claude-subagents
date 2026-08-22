#!/usr/bin/env bash
# pool.sh (library) — read and validate a reviewer/coder pool file.
# Source this; do not execute it. Every function writes diagnostics to stderr only.

POOL_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POOL_PLUGIN_ROOT="$(cd "$POOL_LIB_DIR/../.." && pwd)"
POOL_HARNESS_DIR="$POOL_PLUGIN_ROOT/scripts/harness"

pool_die() { echo "error: $*" >&2; exit 2; }

# pool_file_for <role> -> echoes the pool file path for reviewer(s) or coder(s).
pool_file_for() {
  case "$1" in
    reviewer|reviewers) echo "${CSC_REVIEWERS_JSON:-$POOL_PLUGIN_ROOT/.claude-plugin/reviewers.json}" ;;
    coder|coders)       echo "${CSC_CODERS_JSON:-$POOL_PLUGIN_ROOT/.claude-plugin/coders.json}" ;;
    *) pool_die "unknown role '$1' (expected reviewer or coder)" ;;
  esac
}

# pool_role_key <role> -> the required top-level JSON key, always plural.
pool_role_key() {
  case "$1" in
    reviewer|reviewers) echo reviewers ;;
    coder|coders)       echo coders ;;
    *) pool_die "unknown role '$1'" ;;
  esac
}

# pool_load <file> <role> -> validates fully, sets POOL_JSON and POOL_ROLE_KEY.
# Every failure exits 2 with a message naming the entry and the field.
pool_load() {
  local file="$1" role="$2" key n i
  key="$(pool_role_key "$role")"
  POOL_ROLE_KEY="$key"

  [[ -r "$file" ]] || pool_die "pool file missing or unreadable: $file"
  jq -e . "$file" >/dev/null 2>&1 || pool_die "pool file is not valid JSON: $file"

  [[ "$(jq -r 'type' "$file")" == "object" ]] \
    || pool_die "$file: top level must be a JSON object"
  [[ "$(jq -r --arg k "$key" '[keys[]|select(. != $k)]|length' "$file")" == "0" ]] \
    && [[ "$(jq -r --arg k "$key" 'has($k)' "$file")" == "true" ]] \
    || pool_die "$file: top level must have exactly one key, \"$key\""
  [[ "$(jq -r --arg k "$key" '.[$k]|type' "$file")" == "array" ]] \
    || pool_die "$file: .$key must be an array"

  n="$(jq -r --arg k "$key" '.[$k]|length' "$file")"
  [[ "$n" -gt 0 ]] || pool_die "$file: .$key must not be empty"

  local seen_keys="" defaults=0
  for (( i=0; i<n; i++ )); do
    local e ekey elabel eharness emodel edefault extra
    e="$(jq -c --arg k "$key" --argjson i "$i" '.[$k][$i]' "$file")"
    [[ "$(jq -r 'type' <<<"$e")" == "object" ]] \
      || pool_die "$file: entry $i is not an object"

    extra="$(jq -r '[keys[]|select(. as $x | ["key","label","harness","model","default"]|index($x)|not)]|join(",")' <<<"$e")"
    [[ -z "$extra" ]] || pool_die "$file: entry $i has unknown field(s): $extra"

    for f in key label harness model; do
      [[ "$(jq -r --arg f "$f" 'has($f)' <<<"$e")" == "true" ]] \
        || pool_die "$file: entry $i is missing required field '$f'"
      [[ "$(jq -r --arg f "$f" '.[$f]|type' <<<"$e")" == "string" ]] \
        || pool_die "$file: entry $i field '$f' must be a string"
    done

    ekey="$(jq -r '.key' <<<"$e")"
    elabel="$(jq -r '.label' <<<"$e")"
    eharness="$(jq -r '.harness' <<<"$e")"
    emodel="$(jq -r '.model' <<<"$e")"

    [[ "$ekey"     =~ ^[a-z0-9][a-z0-9-]*$ ]]   || pool_die "$file: entry $i key '$ekey' must match ^[a-z0-9][a-z0-9-]*$"
    [[ "$eharness" =~ ^[a-z][a-z0-9-]*$ ]]      || pool_die "$file: entry $i harness '$eharness' must match ^[a-z][a-z0-9-]*$"
    [[ "$emodel"   =~ ^[A-Za-z0-9._+-]+$ ]]     || pool_die "$file: entry $i model '$emodel' must match ^[A-Za-z0-9._+-]+$"
    [[ -n "$elabel" ]]                          || pool_die "$file: entry $i label must not be empty"
    [[ "$elabel" != *$'\n'* ]]                  || pool_die "$file: entry $i label must not contain a newline"
    LC_ALL=C grep -q '[^ -~]' <<<"$elabel" \
      && pool_die "$file: entry $i label '$elabel' must be printable ASCII"

    [[ -r "$POOL_HARNESS_DIR/$eharness.sh" ]] \
      || pool_die "$file: entry $i harness file not found: $POOL_HARNESS_DIR/$eharness.sh"

    case " $seen_keys " in
      *" $ekey "*) pool_die "$file: duplicate key '$ekey'" ;;
    esac
    seen_keys="$seen_keys $ekey"

    if [[ "$(jq -r 'has("default")' <<<"$e")" == "true" ]]; then
      edefault="$(jq -r '.default|type' <<<"$e")"
      [[ "$edefault" == "boolean" ]] || pool_die "$file: entry $i field 'default' must be a boolean"
      [[ "$(jq -r '.default' <<<"$e")" == "true" ]] && defaults=$((defaults+1))
    fi
  done

  [[ "$defaults" -le 1 ]] || pool_die "$file: at most one entry may be marked default (found $defaults)"

  POOL_JSON="$(cat "$file")"
}

# pool_list_json -> one JSON line: role and entries, WITHOUT the model field.
# The model is withheld on purpose so a model name cannot reach the orchestrator.
pool_list_json() {
  jq -c --arg role "$POOL_ROLE_KEY" \
    '{role:$role, entries:[.[$role][]|{key,label,harness,default:(.default // false)}]}' \
    <<<"$POOL_JSON"
}

# pool_get <key> -> sets ENTRY_KEY/ENTRY_LABEL/ENTRY_HARNESS/ENTRY_MODEL.
pool_get() {
  local want="$1" e
  e="$(jq -c --arg role "$POOL_ROLE_KEY" --arg k "$want" \
        '.[$role][]|select(.key == $k)' <<<"$POOL_JSON")"
  if [[ -z "$e" ]]; then
    local valid
    valid="$(jq -r --arg role "$POOL_ROLE_KEY" '[.[$role][].key]|join(", ")' <<<"$POOL_JSON")"
    pool_die "unknown key '$want'. Valid keys: $valid"
  fi
  ENTRY_KEY="$(jq -r '.key' <<<"$e")"
  ENTRY_LABEL="$(jq -r '.label' <<<"$e")"
  ENTRY_HARNESS="$(jq -r '.harness' <<<"$e")"
  ENTRY_MODEL="$(jq -r '.model' <<<"$e")"
}
