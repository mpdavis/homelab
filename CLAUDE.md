# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

GitOps repository for a homelab k3s cluster managed by ArgoCD. Replaces a previous single-host Docker Compose setup (deployed via doco-cd). The full design is in `docs/design.md`.

## Architecture

- **Proxmox** hypervisor on two physical nodes
- **k3s** for Kubernetes orchestration (server on Node 2, agents on both nodes)
- **ArgoCD** watches this repo and reconciles cluster state from committed manifests
- **GPU node** (Node 2) with NVIDIA passthrough for AI inference (Ollama, Open WebUI)
- **Unifi NAS** provides NFS storage for media and bulk data

## Repository Layout

```
apps/           # Per-service manifests (one directory per app, auto-discovered by ArgoCD ApplicationSet)
infra/          # Cluster infrastructure (ArgoCD, cert-manager, ESO, MetalLB, NFS provisioner, nvidia-device-plugin)
clusters/       # ArgoCD ApplicationSet definitions pointing at apps/ and infra/
docs/           # Design documents
```

## Manifest Strategy

- **Helm charts** for third-party software with official charts (Grafana, Prometheus, Traefik, ArgoCD, cert-manager)
- **Kustomize** for custom deployments or apps without good charts (starr apps, IPTV stack)
- Each app directory should be self-contained — ArgoCD ApplicationSet auto-discovers new directories

## Storage Classes

| StorageClass | Backing | Use Case |
|---|---|---|
| `nfs-nas` | Unifi NAS via NFS CSI | Media, bulk appdata, model weights, backups (ReadWriteMany) |
| `local-path` | Local SSD via k3s local-path-provisioner | Databases (SQLite, Postgres), Prometheus TSDB, Loki WAL (ReadWriteOnce) |
| `longhorn` | Replicated (future) | HA storage if/when needed |

## Secrets

External Secrets Operator syncs from Bitwarden Secrets Manager into Kubernetes Secrets. Secret UUIDs carry over from the previous doco-cd setup.

## Networking

- Ingress: Traefik (bundled with k3s) or alternative, with cert-manager for Let's Encrypt DNS-01 via Cloudflare
- Wildcard cert: `*.mpdavis.com`
- Service discovery: Kubernetes-native DNS (`<service>.<namespace>.svc.cluster.local`)
- External access: MetalLB for LoadBalancer IPs on the LAN

## Key Tools

- `kubectl` for cluster interaction
- `helm` for chart templating/debugging
- `kustomize` (or `kubectl -k`) for kustomize-based apps
- `argocd` CLI for ArgoCD management
