# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

GitOps repository for a homelab k3s cluster managed by FluxCD (via FluxOperator). The full design is in `docs/design.md`.

## Architecture

- **Proxmox VE** on two physical nodes (pve1 + pve2)
- **k3s** for Kubernetes — control plane + general workloads in LXC containers, GPU workloads in a VM
- **FluxCD** watches this repo on GitHub and reconciles cluster state
- **GPU node** (pve2) with NVIDIA RTX 3050 passthrough for AI inference (Ollama, Open WebUI)
- **Unifi NAS** provides NFS storage for media and bulk data

## Repository Layout

```
bootstrap/          # Pre-Flux provisioning and configuration
  ansible/          # Ansible — node configuration, k3s install, Flux bootstrap
  tofu/             # OpenTofu — LXC container + VM provisioning on Proxmox
kubernetes/         # Flux-managed cluster state (sync root)
  apps/             # Per-service manifests, grouped by namespace (ai/, media/, homepage/, hello-world/)
  infrastructure/   # Cluster infrastructure — HelmReleases, HelmRepositories, companion manifests
    sources/        # HelmRepository definitions
    controllers/    # HelmRelease definitions (install CRDs first)
    cert-manager/   # ClusterIssuer + ExternalSecret
    external-secrets/ # ClusterSecretStore + TLS
    external-dns/   # ExternalDNS RBAC + companions (Cloudflare per-service DNS)
    authelia/       # Authelia forward-auth (Traefik middleware)
    metallb/        # IPAddressPool + L2Advertisement
    traefik/        # Certificate + TLSStore
    monitoring/     # Grafana ingress + ExternalSecret + dashboards
    gatus/          # Synthetic-monitoring companions (status page IngressRoute + PrometheusRule)
    flux-operator/  # RBAC + IngressRoute for Flux web UI
    flux-notifications/ # Flux Alert/Provider (GitHub commit status)
  clusters/         # Flux Kustomization entrypoints (infra.yaml, apps.yaml, flux-system/)
docs/               # Design documents
```

## Manifest Strategy

- **HelmRelease** for third-party software with official Helm charts (one per component, values inline)
- **Kustomize** for custom deployments or apps without good charts
- Each infrastructure component is a self-contained directory with its own `kustomization.yaml`
- Dependency chain: `infrastructure-sources` → `infrastructure-controllers` → `infrastructure` → `apps` (via Flux Kustomization `dependsOn`)

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

## Synthetic Monitoring & Deploy Canary

Gatus (`kubernetes/infrastructure/controllers/gatus.yaml`, ns `monitoring`) probes every service
every 60s; the status page is public at `status.mpdavis.com` (no Authelia — the deploy canary
queries it from GitHub Actions). Check conventions:

- Open services: `[STATUS] == 200` + cert expiry (`*open-conditions` anchor)
- Authelia-protected services: `ignore-redirect: true` + `[STATUS] == 302` (`*auth-conditions`) —
  a 200 would mean the forward-auth middleware is missing
- Internal services (no ingress): cluster-DNS health endpoint, `[STATUS] == 200`
- `*.mpdavis.com` probes resolve via a `hostAliases` postRenderers patch to the Traefik VIP
  (no NAT-hairpin dependency)

**When a service gains or loses an IngressRoute, update BOTH lists in `gatus.yaml`:** the
`config.endpoints` entry (correct group/conditions) *and* the hostname in the `hostAliases`
postRenderers patch. The `add-service` skill covers this for new services.

Deploy pipeline: merges are serialized by `deploy-health-gate.yml`, which requires Flux's
`kustomization/apps/<digest>` commit status **and** the `canary/gatus` status posted by
`deploy-canary.yml`. The canary baselines what's already failing before each deploy and only
blames the merge for passing→failing transitions — those auto-open a revert PR; pre-existing
failures are exempt and alert via the `GatusEndpointDown` PrometheusRule instead. Details in
`.github/workflows/README.md`.

## Networking

- Ingress: Traefik as single entry point for all HTTP/HTTPS (k8s and external services)
- MetalLB VIP: `10.0.1.200`
- Wildcard cert: `*.mpdavis.com` via cert-manager (DNS-01, Cloudflare)
- DNS records: ExternalDNS provisions a per-service Cloudflare A record from each IngressRoute's `Host()` rule
- Auth: Authelia forward-auth Traefik middleware protects selected services
- Service discovery: Kubernetes-native DNS (`<service>.<namespace>.svc.cluster.local`)

## Key Tools

- `kubectl` for cluster interaction
- `helm` for chart templating/debugging
- `kustomize` (or `kubectl -k`) for kustomize-based apps
- `flux` CLI for Flux management
