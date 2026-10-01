# Infra host stacks

Same conventions as `../apps/CLAUDE.md`, deployed by the infra host (poll
target `infra`, so `docker/doco-cd/.doco-cd.infra.yaml`) rather than the compose host.

What belongs here: ingress and services for things that do not run on the
compose host, so they keep working while it is down — including Gatus and ntfy,
which have to report on it.

**Public services here are reached through the compose host.** The router
forwards 443 only to the compose host's public Caddy, so a public hostname
served from this host (ntfy, the status page) publishes its port on this
host's LAN address, and `docker/apps/proxy/Caddyfile.public` proxies to it. The
backend survives a compose-host outage; the public route does not.
