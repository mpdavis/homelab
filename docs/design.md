# Homelab Infrastructure Redesign

Ground-up redesign of the homelab infrastructure. Moving from single-host
Docker Compose (deployed via doco-cd) to a multi-node Proxmox + k3s cluster
with ArgoCD-driven GitOps.

## Goals

- Multi-node cluster with central hardware visibility (Proxmox)
- Kubernetes (k3s) for orchestration and service discovery
- GitOps via ArgoCD — push manifests, cluster converges
- GPU-accelerated local AI inference
- Easy to experiment with new services (deploy a Helm chart, done)
- Proper storage tiering: fast local disks for databases, NAS for bulk media

## Hardware

### Node 1 (existing host)

Current bare-metal Debian server running all Docker workloads. Will be
reimaged with Proxmox after workloads are migrated to Node 2.

- Role during migration: runs docker-legacy VM (all current stacks via doco-cd)
- Role post-migration: k3s agent, general workloads

### Node 2 (new host)

New server with NVIDIA GPU for inference workloads.

- Role: k3s server (control plane) + k3s agent with GPU passthrough
- GPU: NVIDIA (targeting 24GB VRAM — RTX 3090, A4000, or A5000)

### Unifi NAS

Existing network-attached storage. Holds all media, bulk appdata, and backups.
Exports via NFS to all cluster nodes.

- Path: `/mnt/nas/data` (current mount convention)
- Media: `/mnt/nas/data/media`
- Appdata: `/mnt/nas/data/appdata`
- Docker legacy: `/mnt/nas/homelab/docker`

## Architecture

```
┌─────────────────────────────────────┐    ┌─────────────────────────────────────┐
│  Node 1 (existing)                  │    │  Node 2 (new, GPU)                  │
│  Proxmox VE                         │    │  Proxmox VE                         │
│                                     │    │                                     │
│  ┌───────────────────────────────┐  │    │  ┌───────────────────────────────┐  │
│  │ VM: docker-legacy             │  │    │  │ VM: k3s-server                │  │
│  │ Debian · Docker · doco-cd     │  │    │  │ k3s control plane + agent     │  │
│  │ (runs during migration only)  │  │    │  │ ArgoCD                        │  │
│  └───────────────────────────────┘  │    │  └───────────────────────────────┘  │
│                                     │    │                                     │
│  ┌───────────────────────────────┐  │    │  ┌───────────────────────────────┐  │
│  │ VM: k3s-agent-1               │  │    │  │ VM: k3s-agent-gpu             │  │
│  │ k3s agent                     │  │    │  │ k3s agent                     │  │
│  │ General workloads             │  │    │  │ NVIDIA GPU passthrough         │  │
│  └───────────────────────────────┘  │    │  │ AI inference pods             │  │
│                                     │    │  └───────────────────────────────┘  │
└─────────────────────────────────────┘    └─────────────────────────────────────┘
                         │                                      │
                         └──────────────┬───────────────────────┘
                                        │ NFS
                              ┌─────────▼─────────┐
                              │    Unifi NAS       │
                              │    NFS exports     │
                              └───────────────────┘
```

## Kubernetes Distribution: k3s

- Lightweight, single-binary Kubernetes
- Built-in: CoreDNS, Traefik ingress controller, local-path-provisioner, metrics-server
- Easy multi-node: `k3s server` on one VM, `k3s agent --server` on others
- Supports HA control plane via embedded etcd (can promote later if needed)

### Cluster Topology

| VM | Host | Role | Notes |
|----|------|------|-------|
| k3s-server | Node 2 | server (control plane + agent) | Runs ArgoCD, cluster infra |
| k3s-agent-gpu | Node 2 | agent | GPU passthrough, AI workloads |
| k3s-agent-1 | Node 1 | agent | General workloads (added after migration) |

Starting with a single server node is fine for a homelab. Can promote to HA
etcd later by adding more server nodes.

## GitOps: ArgoCD

ArgoCD watches this repository and reconciles cluster state from manifests
committed here.

### Why ArgoCD over Flux

- Rich web UI for visualizing deployments, diffs, and sync status
- App-of-apps pattern makes it easy to onboard new services
- Good for experimentation: can manually sync, pause auto-sync, rollback
- Larger community, more Helm chart integrations out of the box

### How It Works

1. Push manifests/Helm values to this repo
2. ArgoCD detects the change (webhook or polling)
3. ArgoCD renders the manifests and diffs against live cluster state
4. Auto-sync applies the change (or manual sync if preferred per-app)

## Storage Strategy

Three tiers of storage, matched to workload characteristics:

### Tier 1: NAS (NFS)

For bulk data that doesn't need low-latency random I/O.

- **What**: Media files, large appdata directories, backups, model weights
- **Where**: Unifi NAS, exported via NFS
- **K8s mechanism**: NFS CSI driver (e.g., `nfs-subdir-external-provisioner`) or static PVs
- **Access mode**: ReadWriteMany (multiple pods can mount simultaneously)
- **StorageClass name**: `nfs-nas`

Workloads using this tier:
- Jellyfin / Emby (media library)
- Sonarr / Radarr (downloads, media management)
- qBittorrent (download directory)
- Grafana (dashboard storage — low write volume)
- AI model weights (read-heavy, large files)

### Tier 2: Local SSD

For latency-sensitive, random-I/O workloads. Data lives on the VM's local
disk. Not replicated — rely on backups.

- **What**: SQL databases, SQLite files, Prometheus TSDB, Loki WAL/index
- **Where**: Local SSD on the Proxmox host, passed through to VM disk
- **K8s mechanism**: `local-path-provisioner` (bundled with k3s)
- **Access mode**: ReadWriteOnce (pinned to the node where the PV lives)
- **StorageClass name**: `local-path`

Workloads using this tier:
- PostgreSQL (any service that needs a relational DB)
- Sonarr / Radarr SQLite databases (suffer badly on NFS)
- Prometheus TSDB (high write throughput)
- Loki WAL and index (latency-sensitive writes)
- Open WebUI data (SQLite backend)

**Backup strategy**: Scheduled CronJobs that dump databases and copy to NAS.

### Tier 3: Replicated (future, optional)

For workloads where you want data to survive a node failure without manual
restore. Not needed at first — add when you have a reason.

- **What**: Anything requiring HA storage
- **Where**: Replicated across nodes via Longhorn or Rook-Ceph
- **K8s mechanism**: Longhorn CSI driver
- **Access mode**: ReadWriteOnce (with replication factor 2-3)
- **StorageClass name**: `longhorn`

## Networking

### Ingress

Replace the current Traefik-on-Docker setup with Traefik running as the k3s
ingress controller (it's bundled by default). Alternatively, swap to
ingress-nginx or Cilium's Gateway API implementation.

Key requirements:
- TLS termination with Let's Encrypt (DNS-01 via Cloudflare, same as today)
- Wildcard cert for `*.mpdavis.com`
- Stable ingress IP on the LAN (MetalLB or `hostNetwork: true` on a known node)

### Service discovery

K8s native: services find each other via DNS (`<service>.<namespace>.svc.cluster.local`).
No more `HOST_IP` variable or hardcoded IPs in compose files.

### External access

- MetalLB assigns real LAN IPs to `LoadBalancer`-type services
- Or: single ingress IP handles all HTTP(S), TCP services get NodePort or dedicated LB IP

### DNS

Keep Cloudflare as the authoritative DNS for `mpdavis.com`. Point `*.mpdavis.com`
at the ingress IP (MetalLB VIP or node IP). Same as today, just a different target IP.

## Secrets Management

### External Secrets Operator (ESO)

Continue using Bitwarden Secrets Manager as the source of truth. ESO syncs
BWSM secrets into Kubernetes Secrets automatically.

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: grafana-admin
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: grafana-admin
  data:
    - secretKey: password
      remoteRef:
        key: 84b2fe83-0a83-4b84-8f5d-b3e001210112
```

Same BWSM UUIDs from `.doco-cd.yaml` carry over directly — no secret rotation
needed during migration (unless you want to).

## GPU Setup

### Proxmox GPU Passthrough

1. Enable IOMMU in BIOS and Proxmox kernel params (`intel_iommu=on` or `amd_iommu=on`)
2. Blacklist nouveau/nvidia on the Proxmox host (GPU is for the VM, not the hypervisor)
3. Add GPU PCI device to the `k3s-agent-gpu` VM configuration
4. VM sees the GPU as a native device

### Kubernetes GPU Scheduling

1. Install NVIDIA drivers + `nvidia-container-toolkit` in the GPU VM
2. Deploy `nvidia-device-plugin` DaemonSet — advertises `nvidia.com/gpu` resource
3. Pods request GPU via resource limits:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

### AI Inference Stack

- **Ollama**: Easy model management, OpenAI-compatible API, runs llama.cpp under the hood
- **Open WebUI**: Chat interface (already running, just needs to point at Ollama)
- **vLLM** (optional): Higher throughput for serving, PagedAttention, continuous batching

Model storage goes on NAS (Tier 1) — models are large but read-sequentially at
load time. Inference scratch/KV cache uses local memory/GPU VRAM.

## Migration Plan

Parallel migration: existing Docker host stays running while k3s cluster is
built up on the new hardware. Services move one at a time.

### Phase 0: Foundation

- [ ] Purchase and rack Node 2
- [ ] Install Proxmox VE on Node 2
- [ ] Create VMs: `k3s-server`, `k3s-agent-gpu`
- [ ] Install k3s (server + agent)
- [ ] Deploy ArgoCD (self-managing from this repo)
- [ ] Set up NFS mounts from Unifi NAS to k3s nodes
- [ ] Install NFS CSI provisioner
- [ ] Set up External Secrets Operator + Bitwarden SecretStore
- [ ] Install NVIDIA drivers + device plugin on GPU node
- [ ] Configure MetalLB or decide on ingress IP strategy

### Phase 1: First Services (low-risk, validate pipeline)

- [ ] Migrate `homepage` — stateless, fast validation of full GitOps flow
- [ ] Migrate `ai` (Open WebUI + Ollama) — gets it onto GPU, biggest benefit
- [ ] Migrate `devbox` — low-stakes, good test of persistent storage

### Phase 2: Observability

- [ ] Deploy Prometheus + Grafana + Loki on k3s
- [ ] Prometheus TSDB on local-path storage
- [ ] Loki on local-path storage (WAL/index) + NFS (chunks)
- [ ] Grafana dashboards on NFS
- [ ] Promtail replaced by k8s-native log collection (Promtail DaemonSet or Alloy)
- [ ] Validate dashboards work, then decommission Docker observability stack

### Phase 3: Ingress Cutover

- [ ] Deploy Traefik (or alternative) as k3s ingress
- [ ] Configure cert-manager with Cloudflare DNS-01 (replaces Traefik's built-in ACME)
- [ ] Migrate `*.mpdavis.com` DNS to point at k3s ingress IP
- [ ] Verify all IngressRoute/Ingress resources are working
- [ ] Decommission Docker Traefik

### Phase 4: Media Stack

- [ ] Migrate `starr` (Sonarr, Radarr, Prowlarr, Seerr, Unpackerr)
  - SQLite databases → local-path PVs (copy from NAS appdata)
  - Media mounts → NFS PVs (same paths, just mounted differently)
- [ ] Migrate `streaming` (Jellyfin, Emby)
  - Config → local-path PV
  - Media → NFS PV (ReadOnly where possible)
- [ ] Migrate `torrent` (Gluetun + qBittorrent)
  - Gluetun as a sidecar container in the qBittorrent pod
  - Downloads directory → NFS PV
- [ ] Migrate `iptv` (Dispatcharr, ECM, Teamarr)

### Phase 5: Consolidation

- [ ] Install Proxmox on Node 1 (wipe bare-metal Debian)
- [ ] Create `k3s-agent-1` VM on Node 1
- [ ] Join Node 1 to the cluster
- [ ] Rebalance workloads across both nodes
- [ ] Archive `homelab-compose` repo (or keep for reference)
- [ ] Final DNS/networking cleanup

## Repository Structure

```
homelab/
├── docs/
│   └── design.md              ← this file
├── apps/
│   ├── homepage/
│   │   ├── kustomization.yaml
│   │   └── ...
│   ├── open-webui/
│   ├── ollama/
│   ├── grafana/
│   ├── prometheus/
│   ├── loki/
│   ├── jellyfin/
│   ├── sonarr/
│   ├── radarr/
│   ├── prowlarr/
│   ├── qbittorrent/
│   ├── traefik/
│   └── ...
├── infra/
│   ├── argocd/
│   ├── cert-manager/
│   ├── external-secrets/
│   ├── metallb/
│   ├── nfs-provisioner/
│   ├── nvidia-device-plugin/
│   └── ...
├── clusters/
│   └── homelab/
│       ├── apps.yaml          # ArgoCD ApplicationSet → apps/
│       └── infra.yaml         # ArgoCD ApplicationSet → infra/
└── README.md
```

### Manifest Strategy

- **Helm charts** for third-party software with official charts (Grafana, Prometheus, Traefik, ArgoCD, cert-manager)
- **Kustomize** for custom deployments or apps without good charts (starr apps, IPTV stack)
- Each app directory is self-contained: ArgoCD ApplicationSet auto-discovers new directories

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-05-16 | k3s over kubeadm/Talos | Lightweight, batteries-included, great for homelab scale |
| 2026-05-16 | ArgoCD over Flux | Web UI for experimentation, app-of-apps pattern |
| 2026-05-16 | Separate repo from homelab-compose | Clean break, no legacy baggage in the fleet repo |
| 2026-05-16 | Parallel migration (not hard cutover) | Lower risk, can validate each service before decommissioning Docker |
| 2026-05-16 | External Secrets Operator + BWSM | Continuity with existing secret management, same UUIDs |
| 2026-05-16 | NVIDIA GPU for inference | Local LLM serving (Ollama), 24GB VRAM class |
| 2026-05-16 | Local-path for databases, NFS for media | SQLite/Postgres need low-latency I/O; media is bulk sequential reads |

## Open Questions

- [ ] Exact hardware spec for Node 2 (CPU, RAM, case, PSU for GPU)
- [ ] Which NVIDIA card specifically (3090 vs A4000 vs A5000 — price/noise/reliability tradeoff)
- [ ] Single k3s server node or HA (3 servers) from the start?
- [ ] Keep Traefik as ingress or switch to ingress-nginx / Cilium Gateway API?
- [ ] Longhorn from day one or add it later when there's a real HA need?
- [ ] How to handle the VPN/Gluetun requirement for torrent in K8s (sidecar vs gateway pod)
