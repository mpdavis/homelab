#!/usr/bin/env bash
# Offline checks for the compose side of the repo — what doco-cd would
# otherwise only discover on the host after merge:
#   - every compose file under the stack trees and doco-cd/ renders
#   - every external_secrets entry is shaped like a Bitwarden UUID
#   - every stack tree has a doco-cd deploy config that discovers it
#   - every Caddyfile parses with the Caddy build the host will run
#   - no hostname is served by more than one Caddyfile on a host, since a
#     public/tailnet duplicate would silently expose a tailnet site
set -euo pipefail

# One tree per host: stacks/ is the compose host, infra/ the infra host.
trees=(stacks infra)
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
done < <(for f in "$doco"/.doco-cd.yaml "$doco"/.doco-cd.*.yaml; do
  if [ -f "$f" ]; then check_secret_ids "$f"; fi
done
for tree in "${trees[@]}"; do
  for f in "$tree"/*/.doco-cd.yml; do
    if [ -f "$f" ]; then check_secret_ids "$f"; fi
  done
done)

for compose in "$doco"/compose*.yaml; do
  render "$compose" "$doco"/.doco-cd.yaml "$doco"/.doco-cd.*.yaml
done

for tree in "${trees[@]}"; do
  # stacks/ is the default deploy config; every other tree needs a target.
  cfg="$doco/.doco-cd.yaml"
  [ "$tree" = stacks ] || cfg="$doco/.doco-cd.$tree.yaml"
  grep -q "^working_dir: $tree\$" "$cfg" 2>/dev/null ||
    fail "$cfg does not discover $tree/, so nothing would deploy it"
done

for tree in "${trees[@]}"; do
  for compose in "$tree"/*/compose.yaml; do
    [ -f "$compose" ] || continue
    render "$compose" "$(dirname "$compose")/.doco-cd.yml"
  done
done

for tree in "${trees[@]}"; do
  proxy="$tree/proxy"
  [ -d "$proxy" ] || continue
  # Validate against the image the stack actually pins, not a stock Caddy:
  # the Caddyfiles use the Cloudflare DNS module, which only that build has.
  image=$(sed -n 's/^ *image: *\(ghcr.io\/mpdavis\/caddy-cloudflare[^ ]*\)/\1/p' "$proxy/compose.yaml" | head -1)
  if [ -z "$image" ]; then
    fail "$proxy/compose.yaml pins no caddy image"
    continue
  fi

  for caddyfile in "$proxy"/Caddyfile.*; do
    if out=$(docker run --rm -e CLOUDFLARE_API_TOKEN="$fake_cf_token" \
      -v "$PWD/$caddyfile:/etc/caddy/Caddyfile:ro" "$image" \
      caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1); then
      echo "ok   $caddyfile"
    else
      printf '%s\n' "$out"
      fail "$caddyfile does not validate"
    fi
  done

  dupes=$(grep -hoE '^[a-z0-9*][a-z0-9.*-]*\.[a-z]+' "$proxy"/Caddyfile.* | sort | uniq -d)
  [ -z "$dupes" ] || fail "$proxy serves these hostnames from more than one Caddyfile: $dupes"
done

# A hostname may only be served by one host, whichever Caddyfile it is in.
caddyfiles=()
for tree in "${trees[@]}"; do
  for f in "$tree"/proxy/Caddyfile.*; do
    [ -f "$f" ] && caddyfiles+=("$f")
  done
done
across=$(grep -hoE '^[a-z0-9*][a-z0-9.*-]*\.[a-z]+' "${caddyfiles[@]}" | sort | uniq -d)
[ -z "$across" ] || fail "these hostnames are served by more than one host: $across"

exit "$status"
