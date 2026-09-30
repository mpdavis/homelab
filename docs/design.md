# Homelab Infrastructure Design

Two Proxmox VE nodes running Docker Compose stacks, deployed from this
repository by doco-cd.

This page is the why. The how lives next to the code:

| Topic | Where |
|---|---|
| Deploy path, host runbook, monitoring, adding a service | `docs/compose.md` |
| Writing a stack: exposure, routing, secrets, pinning | `stacks/CLAUDE.md`, `infra/CLAUDE.md` |
| Proxmox guests, DNS records, alert routing | `tofu/CLAUDE.md` |
| Images built from this repo | `images/CLAUDE.md` |
| The development host | `docs/devbox.md` |

## Goals

- Central hardware visibility (Proxmox)
- GitOps: merge to `main`, the hosts converge
- GPU-accelerated media transcoding and local AI inference
- Easy to add a service: a compose file, a Caddy site, a DNS record
- Fast local disks for databases, the NAS for bulk media
- Alerting that still works when the homelab is down
- Anything the services depend on to exist lives outside them (DNS, monitoring)

## Hardware

### Node 1 — pve1

SFF Lenovo, integrated GPU only, 32 GB RAM. `10.0.1.1`.

Runs what should not depend on pve2: the infra host, the Tailscale subnet
router and the development host.

### Node 2 — pve2

SFF Lenovo, NVIDIA RTX 3050 6GB, 64 GB RAM. `10.0.1.2`.

Runs the compose host, which holds the GPU.

### Unifi NAS

Network-attached storage at `10.0.1.6`, exporting NFSv3 only, to an allowlist
of client IPs.

- `/var/nfs/shared/data/` — media
- `/var/nfs/shared/homelab/` — bulk appdata, model weights, archives

## Architecture

```text
          ┌──────────────────────┐    ┌──────────────────────────┐
          │  pve1                │    │  pve2                    │
          │                      │    │  RTX 3050                │
          │  ┌────────────────┐  │    │  ┌────────────────────┐  │
          │  │ infra     VM   │  │    │  │ docker        VM   │  │
          │  │ Gatus, ntfy,   │  │    │  │ every service,     │  │
          │  │ Caddy for LAN  │  │    │  │ public + tailnet   │  │
          │  │ hosts          │  │    │  │ Caddy, Authentik,  │  │
          │  └────────────────┘  │    │  │ GPU passthrough    │  │
          │  ┌────────────────┐  │    │  └────────────────────┘  │
          │  │ devbox    LXC  │  │    │                          │
          │  └────────────────┘  │    │                          │
          │  ┌────────────────┐  │    │                          │
          │  │ tailscale-     │  │    │                          │
          │  │ router    LXC  │  │    │                          │
          │  └────────────────┘  │    │                          │
          └──────────┬───────────┘    └────────────┬─────────────┘
                     └──────────────┬──────────────┘
                                    │ NFS
                          ┌─────────▼─────────┐
                          │   Unifi NAS        │
                          └───────────────────┘
```

Both Docker hosts are VMs rather than LXCs: Docker in an LXC fights runc and
AppArmor (see the devbox), and GPU passthrough needs a VM anyway.

Each host runs one doco-cd, which polls `main` and deploys its tree: `stacks/`
on the compose host, `infra/` on the infra host. A stack is one compose
project; services are grouped by what they do (`media`, `downloads`, `iptv`,
`ai`, …), not one per stack.

## Storage

- **Databases and app config** live in named volumes on the VM's local disk.
  Embedded single-writer engines (SQLite, DuckDB) belong here rather than on
  NFS, and the choice shapes the service: gridiron's DuckDB takes one writer, so
  its ingest runs as a thread inside the web process and the service must never
  run as two containers.
- **Media and bulk data** are NFS volumes (`driver_opts`, `nfsvers=3`) straight
  from the NAS, mounted with `nocopy` — Docker otherwise seeds an empty volume
  from the image and chowns it, which the NAS refuses.

Nothing is replicated. The NAS holds what matters most (media, documents,
model weights, the council digest's state); local volumes rely on being
rebuildable or on app-level backups.

## Networking

### Exposure

```text
Public:   Internet → Cloudflare DNS (A → public IP) → router forwards 443
                   → caddy-public 10.0.1.56 on the compose host → container
Tailnet:  LAN / Tailscale client → Cloudflare DNS (A → 10.0.1.57 or .58)
                   → Tailscale subnet router (10.0.1.0/24) → caddy-tailnet → container
```

Three Caddy instances, one per host and exposure. A hostname is public or
tailnet-only by which Caddyfile it appears in, so exposure is structural rather
than a label that can be forgotten; CI rejects a hostname served twice. Default
to tailnet unless people off the tailnet need it.

Tailnet-only records are public DNS entries holding private addresses — they
resolve anywhere but route only on the LAN or tailnet. Remote access is the
Tailscale subnet router (`tailscale-router`, `10.0.1.53`) advertising
`10.0.1.0/24`.

The router forwards 443 to one IP, so every public hostname terminates on the
compose host. The infra host's public services (ntfy, the status page) are
proxied there from its LAN ports; their backends survive a compose-host
outage, their public route does not.

TLS is a Let's Encrypt certificate per Caddy, issued by DNS-01 through the
Cloudflare plugin built into `images/caddy-cloudflare`.

### Authentication

Authentik (`stacks/authentik`, `iam.mpdavis.com`) is the identity provider.
Its providers and applications are blueprints in the stack, applied by the
worker on startup.

- **Forward auth** for most services: one domain-level proxy provider on the
  embedded outpost covers `*.mpdavis.com`, so a single login gives SSO across
  every gated site. A Caddy site opts in with `import authentik`.
- **Native OIDC** where the app supports it (Paperless), with the client secret
  from Bitwarden.

### DNS

Cloudflare is authoritative for `mpdavis.com`. Every record is managed in
`tofu/cloudflare` as a hostname mapped to a named target (`public`,
`compose_tailnet`, `infra_tailnet`), applied by hand. There is no wildcard
record: per-service records resolve only hostnames that exist.

### IP address plan

| IP | Host | Purpose |
|----|------|---------|
| 10.0.1.1 | pve1 | Proxmox |
| 10.0.1.2 | pve2 | Proxmox |
| 10.0.1.6 | NAS | NFS |
| 10.0.1.53 | tailscale-router (LXC, pve1) | Tailscale subnet router |
| 10.0.1.54 | devbox (LXC, pve1) | development host |
| 10.0.1.55 | docker (VM 205, pve2) | compose host |
| 10.0.1.56 | docker | public Caddy — the router's 443 target |
| 10.0.1.57 | docker | tailnet Caddy |
| 10.0.1.58 | infra (VM 206, pve1) | infra host and its Caddy |

Addresses come from `network.yaml` (git-ignored), which Tofu and
Ansible both read.

## Secrets

Bitwarden Secrets Manager is the source of truth. A stack's `.doco-cd.yml` maps
environment variables to secret UUIDs; doco-cd resolves them at deploy time
with the host's machine-account token. Nothing secret is in the repo.

## Monitoring

- **Grafana Cloud** (free tier) holds metrics, logs and alert rules. An Alloy
  agent on each host ships container logs and trimmed metrics; the rules in
  `grafana-cloud/rules/` notify through ntfy. Off-site evaluation is the point:
  a dead host still alerts.
- **Gatus** on the infra host probes every service every 60s and publishes the
  status page at `status.mpdavis.com`. Authentik-protected hosts are expected
  to answer with a 302 to the login page, so a missing `import authentik` shows
  up as a failure. `GatusEndpointDown` alerts through Grafana Cloud.
- **ntfy** on the infra host delivers every alert to the phone.

## GPU

The RTX 3050 is passed through (VFIO, `hostpci` mapping `gpu`) to the compose
host. Emby (NVENC/NVDEC) and Ollama request it through CDI
(`driver: cdi`, `device_ids: [nvidia.com/gpu=all]`). The NVIDIA toolkit's
`nvidia-cdi-refresh` unit rewrites the spec when the driver changes, so Docker
needs no runtime configuration. A passthrough VM locks all of its RAM, so
pve2's memory is effectively dedicated to the compose host.

## Development Host

`devbox` (LXC 204, `10.0.1.54`, 4 cores / 8 GB / 40 GB) is an always-on machine
for writing code. Coding agents run on it and [herdr](https://herdr.dev)
attaches over SSH, so a laptop, a phone or any other client drives the same
long-lived sessions.

### Why its own host

herdr's value is that a background server keeps agent processes alive across
disconnects. As a compose stack it would be recreated by every image bump that
doco-cd deploys; an always-on host that restarts whenever a dependency moves is
not an always-on host. Nested Docker and a local working tree are simpler on a
plain host too.

The cost: it is provisioned by Tofu and configured by Ansible, not deployed
from `main`, so it can drift. The Ansible role is idempotent and re-running it
is the correction.

### Shape

- **Provisioning:** `tofu/proxmox` (container `devbox`), then
  `ansible/playbooks/devbox.yml`.
- **Privileged LXC with nesting**, for two reasons: tailscaled needs
  `/dev/net/tun` — passed through by Tofu's `device_passthrough`, exactly as
  for `tailscale-router` — and Docker will not start in a container without
  nesting.
- **Its own tailnet node**, not merely a host behind the subnet router, so it
  stays reachable if the router LXC is down and gets a MagicDNS name. Tailscale
  SSH is enabled alongside ordinary key-based sshd: the tailnet ACL authorises
  phone clients that carry no key material, while sshd still serves herdr and
  Ansible.
- **Repos cloned over HTTPS** with `gh` as the credential helper, so one token
  covers clones and pushes with no per-host deploy key.
- **Agent CLIs pinned** (`claude`, `opencode`) in `roles/devbox/defaults`, with
  Renovate tracking them the same way it tracks image tags.

### Capacity note

pve1's LVM thin pool is 141 GB. The devbox's 40 GB disk is thin-provisioned, so
only written blocks are charged, but a pool that genuinely fills can wedge every
guest on the node. `lvs pve/data` is the number to watch; `docs/devbox.md` lists
what to move to NFS first.

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2025-05-16 | Separate repo from homelab-compose | Clean break, no legacy baggage |
| 2025-05-16 | NVIDIA GPU for inference | Local LLM serving via Ollama |
| 2026-07-17 | Gatus for synthetic monitoring | One declarative tool for continuous health checks, alerting through Grafana Cloud |
| 2026-09-15 | Removed the post-merge deploy canary | Too brittle to keep relying on; Gatus probes and `GatusEndpointDown` alerting remain |
| 2026-09-01 | DuckDB (not Postgres) for gridiron, ingest inside the server process | Every query is an analytical scan over ~10M plays, which an embedded columnar engine answers in the time a Postgres round trip would take — no second service, no second volume, backup is one file. The price is a single writer, which is why ingest is an in-process thread and the service must never run as two containers |
| 2026-09-18 | Move from Kubernetes (k3s) to Docker Compose; finished 2026-09-27 | Kubernetes taught what it was meant to; Compose is simpler to run and reason about day to day |
| 2026-09-19 | doco-cd over GitHub Actions pushing deploys | Pull-based: the hosts need no inbound credentials, and a merge converges without a runner |
| 2026-09-22 | Caddy for ingress; two Docker VMs | Caddy's config is short and its per-instance exposure model is structural. pve2 hosts everything, pve1 the few things that must survive pve2 being down |
| 2026-09-25 | Grafana Cloud for monitoring | Off-site alerting is the win: a dead homelab still pages. Self-hosting stays possible — the agent and rules are standard formats |
| 2026-09-27 | Retired homeassistant, minecraft and holmes | Not in use; not worth migrating. Their data is archived on the NAS under `homelab/retired/` |
| 2026-09-27 | GPU moved to the compose host | Emby and Ollama are its only consumers, and both run there |
| 2026-09-30 | doco-cd deploys itself (self-update) | A Renovate bump used to wait for a playbook run, and hosts drifted behind the pinned version. doco-cd now hands over to a new container only once it is healthy |
| 2026-09-30 | One doco-cd per host, not one instance using Docker contexts | A remote context cannot bind-mount files from doco-cd's clone, and most stacks mount their config that way. Inlining them as compose `configs` costs more than a second self-updating instance does |
