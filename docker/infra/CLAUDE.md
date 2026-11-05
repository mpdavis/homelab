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

**Every service here sets `cpus` and `mem_limit`.** The host is a 1 vCPU / 1 GB
VM with no swap, so one runaway service would otherwise take Gatus and ntfy
down with it. Size the memory limit at 3–4× the container's observed peak
(`memory.peak` under `/sys/fs/cgroup/system.slice/docker-<id>.scope/`). doco-cd
is the exception: it is how a broken stack gets fixed, and its compose file is
shared with the compose host.
