#!/bin/sh
# Keeps qbittorrent connectable. Runs in gluetun's network namespace, so both
# APIs are on localhost.
#
# gluetun tries NAT-PMP once per connection and never again: a server that
# refuses it leaves no forwarded port behind a tunnel that otherwise looks fine.
# Cycling the VPN through the control server reconnects (usually to another
# server) inside the same namespace, so qbittorrent and mousehole stay attached
# — unlike a container restart, which strands them until doco-cd restarts them.
set -u

gluetun=http://127.0.0.1:8000
qbittorrent=http://127.0.0.1:8080
interval=60
# Two misses rather than one: gluetun takes ~20s to forward a port after
# connecting. The cooldown leaves room for gluetun's own healthcheck (five
# failures) to take over if a cycle doesn't help.
max_misses=2
cooldown=300

misses=0
last_cycle=0

log() { echo "$(date -Iseconds) $*"; }

json_int() { sed -n "s/.*\"$1\":\([0-9]*\).*/\1/p"; }

set_vpn() {
  curl -fsS --max-time 10 -X PUT -H 'Content-Type: application/json' \
    -d "{\"status\":\"$1\"}" "$gluetun/v1/vpn/status" >/dev/null
}

while :; do
  sleep "$interval"

  port=$(curl -fsS --max-time 5 "$gluetun/v1/portforward" | json_int port)
  if [ -z "$port" ]; then
    log "gluetun control server unreachable"
    continue
  fi

  if [ "$port" -eq 0 ]; then
    misses=$((misses + 1))
    now=$(date +%s)
    if [ "$misses" -ge "$max_misses" ] && [ $((now - last_cycle)) -ge "$cooldown" ]; then
      log "no forwarded port for $misses checks; cycling the VPN"
      set_vpn stopped || log "stopping the VPN failed"
      sleep 5
      set_vpn running || { sleep 5; set_vpn running; } || log "starting the VPN failed"
      last_cycle=$now
      misses=0
    fi
    continue
  fi
  misses=0

  # gluetun's up command pushes the port once, and fails if qbittorrent is
  # restarting at that moment.
  listen=$(curl -fsS --max-time 5 "$qbittorrent/api/v2/app/preferences" | json_int listen_port)
  if [ -n "$listen" ] && [ "$listen" != "$port" ]; then
    log "qbittorrent listens on $listen, forwarded port is $port; updating"
    curl -fsS --max-time 5 --data-urlencode "json={\"listen_port\":$port,\"random_port\":false,\"upnp\":false}" \
      "$qbittorrent/api/v2/app/setPreferences" >/dev/null || log "updating qbittorrent failed"
  fi
done
