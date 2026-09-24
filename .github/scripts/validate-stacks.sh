#!/usr/bin/env bash
# Offline checks for the compose side of the repo — what doco-cd would
# otherwise only discover on the host after merge:
#   - every compose file under stacks/ and doco-cd/ renders
#   - every external_secrets entry is shaped like a Bitwarden UUID
#   - doco-cd has the deploy config that discovers stacks/
#   - every Caddyfile parses with the Caddy build the host will run
#   - no hostname is served by more than one Caddyfile on a host, since a
#     public/tailnet duplicate would silently expose a tailnet site
set -euo pipefail

stacks=stacks
doco="doco-cd"
status=0
# The Cloudflare module rejects anything not shaped like a real token (40
# chars of [A-Za-z0-9_-]) at provision time, so "placeholder" fails validate.
fake_cf_token=$(printf 'x%.0s' {1..40})

fail() {
  printf '::error::%s\n' "$*"
  status=1
}

# Variables doco-cd supplies at deploy time (external_secrets and
# environment keys) as NAME=placeholder, so `compose config` can interpolate
# `${VAR:?}` references. doco-cd configs are flat enough that indented
# upper-case keys are exactly those.
placeholders() {
  cat "$@" 2>/dev/null | sed -n 's/^ \{2,\}\([A-Z][A-Z0-9_]*\):.*/\1=placeholder/p' | sort -u
}

check_secret_ids() {
  local file=$1 name id
  awk '/^external_secrets:/ {in_s=1; next} /^[^ #]/ {in_s=0} in_s && /^  [A-Z]/' "$file" |
    while read -r name id; do
      [[ "$id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
        echo "$file: ${name%:} is not a Bitwarden secret UUID: $id"
    done
}

render() {
  local compose=$1
  shift
  local env_args
  mapfile -t env_args < <(placeholders "$@")
  if env "${env_args[@]}" \
    docker compose --project-directory "$(dirname "$compose")" --file "$compose" config --quiet; then
    echo "ok   $compose"
  else
    fail "$compose does not render"
  fi
}

while read -r problem; do
  fail "$problem"
done < <(for f in "$doco"/.doco-cd.yaml "$doco"/.doco-cd.*.yaml "$stacks"/*/.doco-cd.yml; do
  if [ -f "$f" ]; then check_secret_ids "$f"; fi
done)

for compose in "$doco"/compose*.yaml; do
  render "$compose" "$doco"/.doco-cd.yaml "$doco"/.doco-cd.*.yaml
done

grep -q '^working_dir: stacks$' "$doco/.doco-cd.yaml" ||
  fail "$doco/.doco-cd.yaml does not discover stacks/, so nothing would deploy it"

for compose in "$stacks"/*/compose.yaml; do
  render "$compose" "$(dirname "$compose")/.doco-cd.yml"
done

proxy="$stacks/proxy"
if [ -f "$proxy/Dockerfile" ]; then
  docker build --quiet --tag caddy-validate "$proxy" >/dev/null

  for caddyfile in "$proxy"/Caddyfile.*; do
    if out=$(docker run --rm -e CLOUDFLARE_API_TOKEN="$fake_cf_token" \
      -v "$PWD/$caddyfile:/etc/caddy/Caddyfile:ro" caddy-validate \
      caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1); then
      echo "ok   $caddyfile"
    else
      printf '%s\n' "$out"
      fail "$caddyfile does not validate"
    fi
  done

  dupes=$(grep -hoE '^[a-z0-9*][a-z0-9.*-]*\.[a-z]+' "$proxy"/Caddyfile.* | sort | uniq -d)
  [ -z "$dupes" ] || fail "these hostnames are served from more than one Caddyfile: $dupes"
fi

exit "$status"
