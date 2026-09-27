# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` is a symlink to this
file, so Claude Code and any AGENTS.md-aware tool read the same rules. Edit this file;
never replace the symlink with a copy.

## What This Repo Is

GitOps repository for a homelab that runs Docker Compose stacks on two Proxmox VMs.
doco-cd on each host polls `main` and deploys what changed. The design is in
`docs/design.md`; the host runbook is `docs/compose.md`.

## Architecture

- **Proxmox VE** on two physical nodes (pve1 + pve2)
- **Compose host** — VM `docker` on pve2 (10.0.1.55). Runs `stacks/`, and holds the
  NVIDIA RTX 3050 passthrough for Emby transcoding and Ollama
- **Infra host** — VM `infra` on pve1 (10.0.1.58). Runs `infra/`: what must keep
  working while the compose host is down (Gatus, ntfy, ingress for LAN hosts)
- **Caddy** for ingress: public (10.0.1.56, the router forwards 443 here) and tailnet
  (10.0.1.57) on the compose host, tailnet on the infra host
- **Grafana Cloud** for metrics, logs and alerting; alerts reach the phone via ntfy
- **Unifi NAS** provides NFS storage for media and bulk data

## Repository Layout

```text
bootstrap/          # Provisioning, applied by hand
  ansible/          # Host configuration: Docker, doco-cd, GPU driver, devbox, Proxmox
  tofu/             # OpenTofu — Proxmox guests, Cloudflare DNS, Grafana Cloud routing (see bootstrap/tofu/CLAUDE.md)
stacks/             # Compose stacks for the compose host (see stacks/CLAUDE.md)
infra/              # Compose stacks for the infra host (see infra/CLAUDE.md)
doco-cd/            # doco-cd deploy configs (one per host) + the doco-cd instance itself
images/             # Container images built from this repo (see images/CLAUDE.md)
grafana-cloud/      # Alert rules synced into Grafana Cloud by CI
docs/               # Design (design.md), host runbook (compose.md), devbox runbook (devbox.md)
```

Custom images (gridiron, ames-council-digest) live in their own repos —
`mpdavis/<name>` — which publish `ghcr.io/mpdavis/<name>` from main; Renovate bumps
the pinned tag here like any third-party image. Images with no source of their own
are built from `images/` instead — see `images/CLAUDE.md`.

The `add-service` skill walks through everything a new service needs.

## Comments

Write a comment only when the code cannot explain itself: a non-obvious constraint, a
workaround and the reason for it, an upstream bug, a surprising ordering dependency, a
value that looks wrong but is deliberate. Never restate what a line does, never label a
block with its own name, and never add a comment to a change merely because it is a
change. If a comment would be obvious to someone reading the code, delete it.

This applies to YAML as much as to code — a `# image tag` above `image:` is noise.
Explain *why* a setting, limit, or label is set the way it is, not *that* it is set.

## Storage

- Named Docker volumes on the host's disk for databases and app state (SQLite,
  Postgres). They are named `<stack>_<key>`, so a service's stack decides them.
- NFS volumes on the NAS for media and bulk data, declared in the stack with
  `driver_opts` (`nfsvers=3`, `nocopy`). The NAS exports only to allowlisted IPs.
  See `stacks/CLAUDE.md`.

## Secrets

Never in git. A stack's `.doco-cd.yml` maps env vars to Bitwarden Secrets Manager
UUIDs under `external_secrets`; doco-cd fetches them at deploy time with the host's
machine account. Compose files reference them as `${VAR:?...}` so a missing one
fails the deploy.

## Synthetic Monitoring

Gatus (`infra/gatus/`, on the infra host) probes every service every 60s; the status page
is public at `status.mpdavis.com` (no auth). Failing endpoints alert through Grafana Cloud
(`GatusEndpointDown` in `grafana-cloud/rules/gatus.yml`). Check conventions:

- Open services: `[STATUS] == 200` + cert expiry (`*open-conditions` anchor)
- Authentik-protected services: `ignore-redirect: true` + `Accept: text/html` header
  (`*auth-headers`) + `[STATUS] == 302` (`*auth-conditions`) — a 200 would mean forward
  auth is missing. The 302 proves only that Authentik answers, not that the app is up, so
  if the app has an unauthenticated health path, serve it ahead of `import authentik` in
  its Caddy site and add an `internal` check against it (see `prowlarr-app`)
- `*.mpdavis.com` probes resolve through `extra_hosts` in `infra/gatus/compose.yaml` to
  the Caddy that serves the hostname (no NAT-hairpin dependency); the `dns-*` checks ask
  a public resolver so a missing record still shows

**When a service gains or loses a hostname, or changes exposure, update BOTH lists:** the
endpoint in `infra/gatus/config.yaml` (correct group/conditions) *and* its `extra_hosts`
line in `infra/gatus/compose.yaml`.

## Networking

- Exposure: a hostname is public or tailnet-only by which Caddyfile serves it
  (`stacks/CLAUDE.md`). Default new services to tailnet unless people off the tailnet
  need them. Remote access is via the Tailscale subnet router advertising `10.0.1.0/24`
- Certificates: each Caddy gets its own from Let's Encrypt over DNS-01 (Cloudflare)
- DNS records: `bootstrap/tofu/cloudflare`, applied by hand from the primary checkout.
  Public hostnames point at the router; tailnet ones at a Caddy's LAN address
- Auth: Authentik (`stacks/authentik/`) behind `iam.mpdavis.com`. Caddy sites opt into
  forward auth with `import authentik` (`stacks/proxy/authentik.caddy`, a domain-level
  provider on the embedded outpost); apps that support it use native OIDC (e.g. Paperless)
- Service discovery: containers on the shared `proxy` network reach each other by
  service name

## Key Tools

- `ssh root@10.0.1.55` / `root@10.0.1.58`, then `docker` and `docker compose` for the hosts
- `docker logs doco-cd-doco-cd-1` for what doco-cd deployed or why it failed
- `tofu` and `ansible-playbook` for `bootstrap/`, run from the primary checkout
- `bws` for Bitwarden secrets (pass `--color no` when piping its JSON)
