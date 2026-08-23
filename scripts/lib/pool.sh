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
#
# ONE JSON pass does the parsing; bash does the rest. An earlier version ran about
# ten external validator invocations per entry, which cost 14 seconds for a four-entry file on a
# machine where the validator sits behind a shim. pool_load runs before every delegation, so
# that cost lands on every dispatch.
pool_load() {
  local file="$1" role="$2" key out
  key="$(pool_role_key "$role")"
  POOL_ROLE_KEY="$key"

  [[ -r "$file" ]] || pool_die "pool file missing or unreadable: $file"

  out="$(jq -r --arg k "$key" '
    if type != "object" then "ERR top level must be a JSON object"
    elif ((keys|length) != 1) or (has($k)|not) then "ERR top level must have exactly one key, \"\($k)\""
    elif (.[$k]|type) != "array" then "ERR .\($k) must be an array"
    elif (.[$k]|length) == 0 then "ERR .\($k) must not be empty"
    else
      .[$k] | to_entries[] |
      (.key|tostring) as $i | .value as $e |
      if ($e|type) != "object" then "ERR entry \($i) is not an object"
      else
        [ $i,
          (($e|keys) - ["key","label","harness","model","default"] | join(",")),
          ($e.key|type),     ($e.key     // "" | tostring),
          ($e.label|type),   (($e.label|type) == "string" and ($e.label|test("^[ -~]+$"))),
          ($e.harness|type), ($e.harness // "" | tostring),
          ($e.model|type),   ($e.model   // "" | tostring),
          ($e.default|type), ($e.default // false | tostring)
        ] | @tsv
      end
    end
  ' "$file" 2>/dev/null)" || pool_die "pool file is not valid JSON: $file"

  case "$out" in
    "ERR "*) pool_die "$file: ${out#ERR }" ;;
  esac

  local seen_keys="" defaults=0
  local line i extra ktype ekey ltype label_ok htype eharness mtype emodel dtype dval
  local pair f ty cols

  # A while loop fed by a here-string runs in THIS shell, not a subshell, so
  # pool_die can exit and seen_keys/defaults survive the loop. Do not turn this
  # into a pipeline.
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue

    # An "ERR ..." line can also appear per-entry (a non-object entry).
    case "$line" in
      ERR*) pool_die "$file: ${line#ERR }" ;;
    esac

    # Bash read drops empty tab-separated fields; awk preserves them.
    mapfile -t cols < <(awk -F'\t' '{for (i=1; i<=12; i++) print (i<=NF ? $i : "")}' <<<"$line")
    i="${cols[0]}"
    extra="${cols[1]}"
    ktype="${cols[2]}"
    ekey="${cols[3]}"
    ltype="${cols[4]}"
    label_ok="${cols[5]}"
    htype="${cols[6]}"
    eharness="${cols[7]}"
    mtype="${cols[8]}"
    emodel="${cols[9]}"
    dtype="${cols[10]}"
    dval="${cols[11]}"

    [[ -z "$extra" ]] || pool_die "$file: entry $i has unknown field(s): $extra"

    for pair in "key:$ktype" "label:$ltype" "harness:$htype" "model:$mtype"; do
      f="${pair%%:*}"
      ty="${pair#*:}"
      [[ "$ty" != "null" ]] || pool_die "$file: entry $i is missing required field '$f'"
      [[ "$ty" == "string" ]] || pool_die "$file: entry $i field '$f' must be a string"
    done

    [[ "$ekey"     =~ ^[a-z0-9][a-z0-9-]*$ ]] || pool_die "$file: entry $i key '$ekey' must match ^[a-z0-9][a-z0-9-]*$"
    [[ "$eharness" =~ ^[a-z][a-z0-9-]*$ ]]    || pool_die "$file: entry $i harness '$eharness' must match ^[a-z][a-z0-9-]*$"
    [[ "$emodel"   =~ ^[A-Za-z0-9._+-]+$ ]]   || pool_die "$file: entry $i model '$emodel' must match ^[A-Za-z0-9._+-]+$"
    [[ "$label_ok" == "true" ]] \
      || pool_die "$file: entry $i label must be non-empty printable ASCII on a single line"

    [[ -r "$POOL_HARNESS_DIR/$eharness.sh" ]] \
      || pool_die "$file: entry $i harness file not found: $POOL_HARNESS_DIR/$eharness.sh"

    case " $seen_keys " in
      *" $ekey "*) pool_die "$file: duplicate key '$ekey'" ;;
    esac
    seen_keys="$seen_keys $ekey"

    case "$dtype" in
      null)    ;;
      boolean) [[ "$dval" == "true" ]] && defaults=$((defaults+1)) ;;
      *)       pool_die "$file: entry $i field 'default' must be a boolean" ;;
    esac
  done <<< "$out"

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
  local want="$1" row valid
  row="$(jq -r --arg role "$POOL_ROLE_KEY" --arg k "$want" \
        '[ .[$role][] | select(.key == $k) | .key, .label, .harness, .model ] | @tsv' \
        <<<"$POOL_JSON")"
  if [[ -z "$row" ]]; then
    valid="$(jq -r --arg role "$POOL_ROLE_KEY" '[.[$role][].key]|join(", ")' <<<"$POOL_JSON")"
    pool_die "unknown key '$want'. Valid keys: $valid"
  fi
  IFS=$'\t' read -r ENTRY_KEY ENTRY_LABEL ENTRY_HARNESS ENTRY_MODEL <<< "$row"
}
