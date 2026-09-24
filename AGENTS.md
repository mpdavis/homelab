# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` is a symlink to this
file, so Claude Code and any AGENTS.md-aware tool read the same rules. Edit this file;
never replace the symlink with a copy.

## What This Repo Is

GitOps repository for a homelab k3s cluster managed by FluxCD (via FluxOperator). The full design is in `docs/design.md`.

## Architecture

- **Proxmox VE** on two physical nodes (pve1 + pve2)
- **k3s** for Kubernetes — control plane + general workloads in LXC containers, GPU workloads in a VM
- **FluxCD** watches this repo on GitHub and reconciles cluster state
- **GPU node** (pve2) with NVIDIA RTX 3050 passthrough for AI inference (Ollama, Open WebUI)
- **Unifi NAS** provides NFS storage for media and bulk data

## Repository Layout

```text
bootstrap/          # Pre-Flux provisioning and configuration
  ansible/          # Ansible — node configuration, k3s install, Flux bootstrap, devbox
  tofu/             # OpenTofu — Proxmox guests + Cloudflare DNS (see bootstrap/tofu/CLAUDE.md)
kubernetes/         # Flux-managed cluster state (sync root)
  apps/             # Per-service manifests, grouped by namespace (ai/, civic/, media/, homepage/)
  infrastructure/   # Cluster infrastructure — HelmReleases, HelmRepositories, companion manifests
    sources/        # HelmRepository definitions
    controllers/    # HelmRelease definitions (install CRDs first)
    cert-manager/   # ClusterIssuer + ExternalSecret
    external-secrets/ # ClusterSecretStore + TLS
    external-dns/   # ExternalDNS RBAC + companions (Cloudflare per-service DNS)
    authentik/      # Authentik IdP (HelmRelease, blueprints, forward-auth middleware)
    metallb/        # IPAddressPool + L2Advertisement
    traefik/        # Certificate + TLSStore
    monitoring/     # Grafana ingress + ExternalSecret + dashboards
    gatus/          # Synthetic-monitoring companions (status page IngressRoute + PrometheusRule)
    flux-operator/  # RBAC + IngressRoute for Flux web UI
    flux-notifications/ # Flux Alert/Provider (GitHub commit status)
  clusters/         # Flux Kustomization entrypoints (infra.yaml, apps.yaml, flux-system/)
images/             # Container images built from this repo (see images/CLAUDE.md)
stacks/             # Docker Compose stacks for the compose host (stacks/<stack>/)
infra/              # Docker Compose stacks for the infra host (ingress for non-compose services)
doco-cd/            # doco-cd deploy configs (one per host) + the doco-cd instance itself
docs/               # Design documents (devbox.md = dev-host runbook, compose.md = compose hosts)
```

**Migration in progress:** services are moving from k3s to Docker Compose one at a
time. A service lives in exactly one of `kubernetes/`, `stacks/` or `infra/`, never
more. `stacks/CLAUDE.md` has the rules for writing a stack; `docs/compose.md` has the
deploy path and the host runbook.

Custom images (gridiron, ames-council-digest) live in their own repos —
`mpdavis/<name>` — which publish `ghcr.io/mpdavis/<name>` from main; Renovate bumps
the pinned tag here like any third-party image. Images with no source of their own
are built from `images/` instead — see `images/CLAUDE.md`.

## Manifest Strategy

- **HelmRelease** for third-party software with official Helm charts (one per component, values inline)
- **Kustomize** for custom deployments or apps without good charts
- Each infrastructure component is a self-contained directory with its own `kustomization.yaml`
- Dependency chain: `infrastructure-sources` → `infrastructure-controllers` → `infrastructure` → `apps` (via Flux Kustomization `dependsOn`)

## Comments

Write a comment only when the code cannot explain itself: a non-obvious constraint, a
workaround and the reason for it, an upstream bug, a surprising ordering dependency, a
value that looks wrong but is deliberate. Never restate what a line does, never label a
block with its own name, and never add a comment to a change merely because it is a
change. If a comment would be obvious to someone reading the code, delete it.

This applies to YAML as much as to code — a `# image tag` above `tag:` is noise. Explain
*why* a Helm value, resource limit, or annotation is set the way it is, not *that* it is
set.

## Storage Classes

| StorageClass | Backing | Use Case |
|---|---|---|
| `nfs-data` | Unifi NAS `data` share via NFS provisioner | Media files (ReadWriteMany) |
| `nfs-homelab` | Unifi NAS `homelab` share via NFS provisioner | Bulk appdata, model weights, backups (ReadWriteMany) |
| `local-path` | Local SSD via k3s local-path-provisioner | Databases (SQLite, Postgres), Prometheus TSDB, Loki WAL (ReadWriteOnce) |
| `longhorn` | Replicated (future) | HA storage if/when needed |

## Secrets

External Secrets Operator syncs from Bitwarden Secrets Manager into Kubernetes Secrets. ExternalSecret CRs reference only the secret store and Bitwarden UUIDs — never secret data.

Bitwarden (BWS) secret UUIDs are centralized in the `bws-secret-ids` ConfigMap (`kubernetes/clusters/homelab/flux-system/bws-secret-ids.yaml`). Each UUID is defined once as a `BWS_*` key and referenced from an ExternalSecret's `remoteRef.key` as a `${BWS_*}` placeholder, resolved by Flux postBuild substitution (the same mechanism as `cluster-vars`). Any Flux Kustomization holding an ExternalSecret lists `bws-secret-ids` in its `spec.postBuild.substituteFrom`.

## Synthetic Monitoring

Gatus (`kubernetes/infrastructure/controllers/gatus.yaml`, ns `monitoring`) probes every service
every 60s; the status page is public at `status.mpdavis.com` (no auth). Failing endpoints alert
via the `GatusEndpointDown` PrometheusRule. Check conventions:

- Open services: `[STATUS] == 200` + cert expiry (`*open-conditions` anchor)
- Authentik-protected services: `ignore-redirect: true` + `Accept: text/html` header
  (`*auth-headers`) + `[STATUS] == 302` (`*auth-conditions`) — a 200 would mean the
  forward-auth middleware is missing. The header exercises the browser-style
  redirect path to `iam.mpdavis.com`. The 302 proves only that Authentik answers, not that the
  app is up, so if the app has an unauthenticated health endpoint also add an `internal` check
  against its cluster-DNS Service
- Internal services (no ingress): cluster-DNS health endpoint, `[STATUS] == 200`
- `*.mpdavis.com` probes resolve via a `hostAliases` postRenderers patch to the IP that
  serves the hostname — public Traefik VIP, tailnet Traefik VIP, or the compose host's
  Caddy (`COMPOSE_TAILNET_IP`) for services already migrated (no NAT-hairpin dependency)

**When a service gains or loses an IngressRoute, or changes exposure, update BOTH lists in
`gatus.yaml`:** the `config.endpoints` entry (correct group/conditions) *and* the hostname under
the correct IP in the `hostAliases` postRenderers patch. The `add-service` skill covers this for
new services.

## Networking

- Ingress: Traefik as single entry point for all HTTP/HTTPS (k8s and external services)
- MetalLB VIPs: `10.0.1.200` public Traefik (router port-forwards 443 here), `10.0.1.210` tailnet-only Traefik (never forwarded)
- Exposure: IngressRoutes are **public** by default. Admin/personal tools get the label
  `homelab.mpdavis.com/exposure: tailnet` (+ `entryPoints: [tailnet]`); the root kustomization
  patches then force the `tailnet` entrypoint and a DNS target of the tailnet VIP. Remote access
  is via the Tailscale subnet router advertising `10.0.1.0/24`. Default new services to tailnet
  unless people off the tailnet need them. Never use an IP allowlist for this — Traefik's
  `externalTrafficPolicy: Cluster` SNATs internet traffic to LAN node IPs
- Wildcard cert: `*.mpdavis.com` via cert-manager (DNS-01, Cloudflare)
- DNS records: ExternalDNS provisions a per-service Cloudflare A record from each IngressRoute's `Host()` rule (public IP, or the tailnet VIP for tailnet routes). Services migrated off k3s have no IngressRoute, so their records are managed in `bootstrap/tofu/cloudflare` instead
- Auth: Authentik forward-auth Traefik middleware (`authentik-forward-auth`, domain-level provider on the embedded outpost) protects selected services; native OIDC for apps that support it (e.g. Paperless)
- Service discovery: Kubernetes-native DNS (`<service>.<namespace>.svc.cluster.local`)

## Key Tools

- `kubectl` for cluster interaction
- `helm` for chart templating/debugging
- `kustomize` (or `kubectl -k`) for kustomize-based apps
- `flux` CLI for Flux management
