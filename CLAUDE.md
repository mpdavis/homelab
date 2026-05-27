# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

GitOps repository for a homelab k3s cluster managed by FluxCD (via FluxOperator). Replaces a previous single-host Docker Compose setup (deployed via doco-cd). The full design is in `docs/design.md`.

## Architecture

- **Proxmox VE** on two physical nodes (pve1 + pve2)
- **k3s** for Kubernetes — control plane + general workloads in LXC containers, GPU workloads in a VM
- **FluxCD** watches this repo on GitHub and reconciles cluster state
- **GPU node** (pve2) with NVIDIA RTX 3050 passthrough for AI inference (Ollama, Open WebUI)
- **Unifi NAS** provides NFS storage for media and bulk data

## Repository Layout

```
apps/               # Per-service manifests (one directory per app)
infrastructure/     # Cluster infrastructure — HelmReleases, HelmRepositories, companion manifests
  sources/          # HelmRepository definitions
  cert-manager/     # HelmRelease + ClusterIssuer + ExternalSecret
  external-secrets/ # HelmRelease + ClusterSecretStore
  metallb/          # HelmRelease + IPAddressPool + L2Advertisement
  traefik/          # HelmRelease + Certificate + TLSStore
  monitoring/       # HelmRelease (kube-prometheus-stack) + Grafana ingress
  loki/             # Loki HelmRelease + Promtail HelmRelease
  nfs-data/         # HelmRelease (nfs-subdir-external-provisioner)
  nfs-homelab/      # HelmRelease (nfs-subdir-external-provisioner)
clusters/           # Flux Kustomization entrypoints (infra.yaml, apps.yaml, flux-system/)
tofu/               # OpenTofu — LXC container + VM provisioning on Proxmox
ansible/            # Ansible — node configuration, k3s install, Flux bootstrap
docs/               # Design documents
```

## Manifest Strategy

- **HelmRelease** for third-party software with official Helm charts (one per component, values inline)
- **Kustomize** for custom deployments or apps without good charts
- Each infrastructure component is a self-contained directory with its own `kustomization.yaml`
- Dependency chain: `infrastructure-sources` → `infrastructure` → `apps` (via Flux Kustomization `dependsOn`)

## Storage Classes

| StorageClass | Backing | Use Case |
|---|---|---|
| `nfs-nas` | Unifi NAS via NFS CSI | Media, bulk appdata, model weights, backups (ReadWriteMany) |
| `local-path` | Local SSD via k3s local-path-provisioner | Databases (SQLite, Postgres), Prometheus TSDB, Loki WAL (ReadWriteOnce) |
| `longhorn` | Replicated (future) | HA storage if/when needed |

## Secrets

External Secrets Operator syncs from Bitwarden Secrets Manager into Kubernetes Secrets. Secret UUIDs carry over from the previous doco-cd setup.

## Networking

- Ingress: Traefik as single entry point for all HTTP/HTTPS (k8s and external services)
- MetalLB VIP: `10.0.1.60`
- Wildcard cert: `*.mpdavis.com` via cert-manager (DNS-01, Cloudflare)
- Service discovery: Kubernetes-native DNS (`<service>.<namespace>.svc.cluster.local`)

## Key Tools

- `kubectl` for cluster interaction
- `helm` for chart templating/debugging
- `kustomize` (or `kubectl -k`) for kustomize-based apps
- `flux` CLI for Flux management
