#!/usr/bin/env bash
#
# Store a secret in Bitwarden Secrets Manager and print its UUID.
#
# The VALUE is read from stdin — never pass it as an argument, so it stays out
# of shell history and out of this script's argv.
#
#   printf '%s' 'abc123' | bws-save-secret.sh BAZARR_API_KEY \
#       --note 'Bazarr API key (Settings -> General -> API Key)'
#
# Flags:
#   --note TEXT         provenance note: where this value came from and how to
#                       regenerate it. Strongly recommended.
#   --project-id UUID   override the homelab project (default below).
#   --rotate            allow replacing the value of a key that already exists.
#   --legacy-key        skip the UPPER_SNAKE_CASE check, for rotating one of the
#                       older entries named like 'Sonarr - API Key'.
#
# Prints exactly two lines on stdout:
#   id=<uuid>
#   sha256=<12-hex fingerprint of the stored value>
# Progress and hints go to stderr. The secret value is never printed.

set -euo pipefail

# The homelab project in BWS. Must match `projectID` in
# kubernetes/infrastructure/external-secrets/clustersecretstore.yaml — External
# Secrets Operator can only read secrets that live in this project.
DEFAULT_PROJECT_ID="07445573-1608-4b6b-8f80-b3e0012078de"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*" >&2; }

# bws colorizes its JSON even when stdout is a pipe, which makes jq choke with
# "Invalid numeric literal". Force color off on every machine-read call.
bwsj() { bws --color no --output json "$@"; }

KEY=""
NOTE=""
ROTATE=0
LEGACY_KEY=0
PROJECT_ID="${BWS_PROJECT_ID:-$DEFAULT_PROJECT_ID}"

while [ $# -gt 0 ]; do
  case "$1" in
    --note)       NOTE="${2:-}"; shift 2 ;;
    --project-id) PROJECT_ID="${2:-}"; shift 2 ;;
    --rotate)     ROTATE=1; shift ;;
    --legacy-key) LEGACY_KEY=1; shift ;;
    -h|--help)    sed -n '3,23p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    -*)           die "unknown flag: $1" ;;
    *)
      if [ -n "$KEY" ]; then die "unexpected argument: $1"; fi
      KEY="$1"; shift ;;
  esac
done

[ -n "$KEY" ] || die "missing secret key name (e.g. SONARR_API_KEY)"
if [ "$LEGACY_KEY" -eq 0 ] && ! printf '%s' "$KEY" | grep -qE '^[A-Z][A-Z0-9_]*$'; then
  die "key must be UPPER_SNAKE_CASE, got: $KEY (use --legacy-key to rotate an older entry such as 'Sonarr - API Key')"
fi
case "$KEY" in
  BWS_*) die "drop the BWS_ prefix — that belongs only to the ConfigMap key" ;;
esac

command -v bws >/dev/null 2>&1 \
  || die "bws not found on PATH — install it (brew install bitwarden/tap/bws)"
[ -n "${BWS_ACCESS_TOKEN:-}" ] \
  || die "BWS_ACCESS_TOKEN is not set — export a machine-account access token with write access to the homelab project"

if [ -t 0 ]; then
  die "the secret value must be piped on stdin, not passed as an argument"
fi
VALUE="$(cat)"   # command substitution strips trailing newlines
[ -n "$VALUE" ] || die "empty secret value on stdin"

fingerprint() { printf '%s' "$1" | shasum -a 256 | cut -c1-12; }

json_get() {  # json_get FIELD  <<< object
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg f "$1" '.[$f] // empty'
  else
    python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$1"
  fi
}

find_secret_id() {  # find_secret_id KEY  <<< array
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$1" 'map(select(.key == $k)) | (.[0].id // empty)'
  else
    python3 -c '
import json, sys
key = sys.argv[1]
for s in json.load(sys.stdin):
    if s.get("key") == key:
        print(s.get("id", "")); break
' "$1"
  fi
}

# --- does this key already exist in the project? -----------------------------
EXISTING_ID=""
if LIST_JSON="$(bwsj secret list "$PROJECT_ID" 2>/dev/null)"; then
  EXISTING_ID="$(printf '%s' "$LIST_JSON" | find_secret_id "$KEY" || true)"
fi

if [ -n "$EXISTING_ID" ] && [ "$ROTATE" -eq 0 ]; then
  note "refusing to overwrite: '$KEY' already exists as $EXISTING_ID"
  note "re-run with --rotate to replace its value. The UUID does not change on a"
  note "rotation, so no ConfigMap or ExternalSecret edit is needed."
  printf 'id=%s\n' "$EXISTING_ID"
  printf 'sha256=%s\n' "existing-unchanged"
  exit 3
fi

# `bws secret create` has shipped both a positional and a flag form across
# releases; detect which one this binary speaks rather than assuming.
CREATE_HELP="$(bws --color no secret create --help 2>&1 || true)"

OUT=""
if [ -n "$EXISTING_ID" ]; then
  note "rotating existing secret '$KEY' ($EXISTING_ID)"
  args=(--value "$VALUE")
  if [ -n "$NOTE" ]; then args+=(--note "$NOTE"); fi
  if ! OUT="$(bwsj secret edit "$EXISTING_ID" "${args[@]}")"; then
    bws secret edit --help >&2 || true
    die "bws secret edit failed — see its usage above and adapt"
  fi
else
  note "creating secret '$KEY' in project $PROJECT_ID"
  if printf '%s' "$CREATE_HELP" | grep -qE '(^|[[:space:]])--key([[:space:],=]|$)'; then
    args=(--key "$KEY" --value "$VALUE" --project-id "$PROJECT_ID")
  else
    args=("$KEY" "$VALUE" "$PROJECT_ID")
  fi
  if [ -n "$NOTE" ]; then args+=(--note "$NOTE"); fi
  if ! OUT="$(bwsj secret create "${args[@]}")"; then
    printf '%s\n' "$CREATE_HELP" >&2
    die "bws secret create failed — see its usage above and adapt"
  fi
fi

ID="$(printf '%s' "$OUT" | json_get id)"
[ -n "$ID" ] || die "could not read the new secret's id out of the bws response"

# Read it back and compare fingerprints, so a silent truncation or a stray
# newline is caught here rather than by a crash-looping pod three days later.
STORED="$(bwsj secret get "$ID" | json_get value)"
if [ "$(fingerprint "$STORED")" != "$(fingerprint "$VALUE")" ]; then
  die "round-trip check failed: the value stored under $ID does not match stdin"
fi

note ""
note "stored and verified. Register it in:"
note "  kubernetes/clusters/homelab/flux-system/bws-secret-ids.yaml"
note ""
note "  # ${NOTE:-<what this is and where to regenerate it>}"
note "  BWS_${KEY}: \"${ID}\""
note ""

printf 'id=%s\n' "$ID"
printf 'sha256=%s\n' "$(fingerprint "$VALUE")"
