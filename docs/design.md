# Homelab Infrastructure Design

Multi-node homelab running k3s on Proxmox VE with FluxCD-driven GitOps.

## Goals

- Multi-node cluster with central hardware visibility (Proxmox)
- Kubernetes (k3s) for orchestration and service discovery
- GitOps via FluxCD — push manifests, cluster converges
- GPU-accelerated local AI inference
- Easy to experiment with new services (deploy a Helm chart, done)
- Proper storage tiering: fast local disks for databases, NAS for bulk media
- Anything the cluster depends on to exist lives outside the cluster (DNS)

## Hardware

### Node 1 — pve1

SFF Lenovo, integrated GPU only, 32 GB RAM.

- Hostname: `pve1`
- IP: `10.0.1.1`
- Role: Proxmox host for k3s control plane + general workload LXC containers

### Node 2 — pve2

SFF Lenovo, NVIDIA RTX 3050 6GB, 64 GB RAM.

- Hostname: `pve2`
- IP: `10.0.1.2`
- Role: Proxmox host for GPU VM

### Unifi NAS

Existing network-attached storage at `10.0.1.6`. Exports via NFS to all cluster nodes.

- Path: `/var/nfs/shared/`
- Data: `/var/nfs/shared/data/`
- Homelab: `/var/nfs/shared/homelab/`

## Architecture

```
                     ┌─────────────────────────────────────────┐
                     │                  LAN                     │
                     │         DNS: Cloudflare                  │
                     └────────┬──────────────┬─────────────────┘
                              │              │
          ┌───────────────────▼──┐    ┌──────▼───────────────────┐
          │  pve1                │    │  pve2                    │
          │  Proxmox VE         │    │  Proxmox VE              │
          │                      │    │  RTX 3050                │
          │  ┌────────────────┐  │    │                          │
          │  │ k3s-server     │  │    │  ┌────────────────────┐  │
          │  │ LXC            │  │    │  │ k3s-agent-gpu      │  │
          │  │ Control plane  │  │    │  │ VM                  │  │
          │  │ + workloads    │  │    │  │ VFIO GPU pass-thru │  │
          │  └────────────────┘  │    │  │ AI inference        │  │
          │                      │    │  └────────────────────┘  │
          │  ┌────────────────┐  │    │                          │
          │  ┌────────────────┐  │    │                          │
          │  │ k3s-agent-1    │  │    │                          │
          │  │ LXC            │  │    │                          │
          │  │ General        │  │    │                          │
          │  │ workloads      │  │    │                          │
          │  └────────────────┘  │    │                          │
          └──────────┬───────────┘    └──────────┬───────────────┘
                     │                           │
                     └─────────┬─────────────────┘
                               │ NFS
                     ┌─────────▼─────────┐
                     │   Unifi NAS        │
                     └───────────────────┘
```

## Kubernetes Distribution: k3s

- Lightweight, single-binary Kubernetes
- Built-in: CoreDNS, Traefik ingress controller, local-path-provisioner, metrics-server
- Easy multi-node: `k3s server` on one node, `k3s agent --server` on others
- Supports HA control plane via embedded etcd (can promote later if needed)
- k3s server and general agents run in privileged LXC containers (lower overhead than VMs)
- GPU agent runs in a full VM (VFIO passthrough requires it)

### Cluster Topology

| Node | Host | Type | IP | Role |
|------|------|------|----|------|
| k3s-server | pve1 | LXC | 10.0.1.50 | server (control plane + workloads) |
| k3s-agent-1 | pve1 | LXC | 10.0.1.51 | agent, general workloads |
| k3s-agent-gpu | pve2 | VM | 10.0.1.52 | agent, GPU passthrough, AI workloads |

### LXC Requirements for k3s

Privileged containers with: `nesting=true`, `keyctl=true`, AppArmor unconfined,
`/dev/kmsg` symlink, `mount --make-rshared /`.

## GitOps: FluxCD

FluxCD watches this repository on GitHub and reconciles cluster state from
committed manifests.

### Why FluxCD

- Declarative, pull-based GitOps — no UI to maintain or secure
- FluxOperator manages Flux lifecycle via Helm; FluxInstance CR bootstraps the sync
- HelmRelease CRDs for individual Helm charts (no umbrella chart boilerplate)
- Kustomization CRDs with `dependsOn` chains for ordering
- Lightweight — just controllers, no web server or database

### How It Works

1. Push manifests to GitHub
2. Flux source-controller detects the change
3. kustomize-controller / helm-controller reconcile the desired state
4. Dependency chain: `infrastructure-sources` → `infrastructure` → `apps`

## Storage Strategy

Three tiers of storage, matched to workload characteristics:

### Tier 1: NAS (NFS)

For bulk data that doesn't need low-latency random I/O.

- **What**: Media files, large appdata directories, backups, model weights
- **Where**: Unifi NAS, exported via NFS
- **K8s mechanism**: NFS CSI driver (`nfs-subdir-external-provisioner`)
- **Access mode**: ReadWriteMany (multiple pods can mount simultaneously)
- **StorageClass name**: `nfs-nas`

### Tier 2: Local SSD

For latency-sensitive, random-I/O workloads. Data lives on the node's local
disk. Not replicated — rely on backups.

- **What**: SQL databases, SQLite files, Prometheus TSDB, Loki WAL/index
- **Where**: Local SSD on the Proxmox host, passed through to container/VM disk
- **K8s mechanism**: `local-path-provisioner` (bundled with k3s)
- **Access mode**: ReadWriteOnce (pinned to the node where the PV lives)
- **StorageClass name**: `local-path`

### Tier 3: Replicated (future, optional)

For workloads where you want data to survive a node failure without manual restore.

- **What**: Anything requiring HA storage
- **Where**: Replicated across nodes via Longhorn or Rook-Ceph
- **K8s mechanism**: Longhorn CSI driver
- **Access mode**: ReadWriteOnce (with replication factor 2-3)
- **StorageClass name**: `longhorn`

## Networking

### Ingress

```
Internet → Cloudflare DNS (*.mpdavis.com)
         → Router port-forward 443 → MetalLB VIP (10.0.1.60)
         → Traefik (k8s IngressRoute)
         → k8s Services  OR  ExternalName/Endpoints → non-k8s services
```

Traefik inside k8s handles ALL HTTP/HTTPS routing, including services running
outside the cluster. For non-k8s services, a Service+Endpoints pair routes
through Traefik with the same wildcard cert and TLS termination as everything
else.

### Service Discovery

K8s native: services find each other via DNS (`<service>.<namespace>.svc.cluster.local`).

### External Access

MetalLB assigns a VIP (10.0.1.60) to the Traefik LoadBalancer service. All
HTTP(S) traffic routes through this single ingress point.

### DNS

Cloudflare as authoritative DNS for `mpdavis.com`. `*.mpdavis.com` points at
the MetalLB VIP.

### IP Address Plan

| IP | Host | Type | Purpose |
|----|------|------|---------|
| 10.0.1.1 | pve1 | Proxmox host | Hypervisor management |
| 10.0.1.2 | pve2 | Proxmox host | Hypervisor management |
| 10.0.1.6 | NAS | Unifi NAS | NFS storage |
| 10.0.1.50 | k3s-server | LXC on pve1 | k3s control plane + workloads |
| 10.0.1.51 | k3s-agent-1 | LXC on pve1 | k3s general workloads |
| 10.0.1.52 | k3s-agent-gpu | VM on pve2 | k3s GPU workloads |
| 10.0.1.60 | (MetalLB VIP) | Virtual | Traefik LoadBalancer ingress |

## Secrets Management

### External Secrets Operator (ESO)

Bitwarden Secrets Manager as the source of truth. ESO syncs BWSM secrets into
Kubernetes Secrets automatically.

## GPU Setup

### Proxmox GPU Passthrough

1. Enable IOMMU in BIOS and Proxmox kernel params
2. Blacklist nouveau on the Proxmox host
3. Add VFIO modules (`vfio`, `vfio_iommu_type1`, `vfio_pci`)
4. Pass GPU PCI device to the `k3s-agent-gpu` VM

### Kubernetes GPU Scheduling

1. NVIDIA drivers (560) + `nvidia-container-toolkit` installed in the GPU VM via Ansible
2. `nvidia-device-plugin` DaemonSet advertises `nvidia.com/gpu` resource
3. Pods request GPU via resource limits:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

### AI Inference Stack

- **Ollama**: Model management, OpenAI-compatible API
- **Open WebUI**: Chat interface pointing at Ollama

Model storage on NAS (Tier 1). Inference scratch/KV cache uses local memory/GPU VRAM.

## Repository Structure

```
homelab/
├── docs/
│   └── design.md              ← this file
├── bootstrap/                 # Pre-Flux provisioning and configuration
│   ├── tofu/                  # OpenTofu — LXC/VM provisioning
│   └── ansible/               # Ansible — node config, k3s install, Flux bootstrap
├── kubernetes/                # Flux-managed cluster state (sync root)
│   ├── kustomization.yaml     # Entry point — includes only Flux plumbing
│   ├── apps/
│   │   ├── kustomization.yaml
│   │   ├── hello-world/
│   │   └── emby/
│   ├── infrastructure/
│   │   ├── kustomization.yaml
│   │   ├── sources/           # HelmRepository definitions
│   │   ├── controllers/       # HelmRelease definitions
│   │   ├── cert-manager/
│   │   ├── external-secrets/
│   │   ├── metallb/
│   │   ├── traefik/
│   │   ├── monitoring/
│   │   └── flux-operator/
│   └── clusters/
│       └── homelab/
│           ├── flux-system/
│           │   ├── kustomization.yaml
│           │   ├── flux-instance.yaml
│           │   └── cluster-vars.yaml
│           ├── infra.yaml
│           └── apps.yaml
└── README.md
```

### Manifest Strategy

- **HelmRelease** for third-party software with official Helm charts (one per component)
- **Kustomize** for custom deployments or apps without good charts
- Each infrastructure component is a self-contained directory with a kustomization.yaml

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2025-05-16 | k3s over kubeadm/Talos | Lightweight, batteries-included, great for homelab scale |
| 2025-05-16 | Separate repo from homelab-compose | Clean break, no legacy baggage |
| 2025-05-16 | External Secrets Operator + BWSM | Continuity with existing secret management |
| 2025-05-16 | NVIDIA GPU for inference | Local LLM serving via Ollama |
| 2025-05-16 | Local-path for databases, NFS for media | SQLite/Postgres need low-latency I/O; media is bulk reads |
| 2025-05-27 | FluxCD over ArgoCD | Declarative, no UI to maintain, HelmRelease per component |
| 2025-05-27 | LXC containers over VMs | Lower overhead; VM only for GPU node (VFIO requires it) |
| 2025-05-27 | Traefik as single ingress for all services | Routes to both k8s and external services via Service+Endpoints |

## Deploy Sequence

```bash
# Phase 0: Manual Proxmox reinstall on both nodes
#   pve1 at 10.0.1.1 (no GPU, 32GB)
#   pve2 at 10.0.1.2 (RTX 3050, 64GB)
#   Create API tokens, enable IOMMU on pve2

# Phase 1: Provision infrastructure
tofu apply                                        # create LXC containers + GPU VM

# Phase 2: Configure nodes
ansible-playbook playbooks/site.yml                # install k3s on all 3 nodes

# Phase 3: Bootstrap cluster services
ansible-playbook playbooks/bootstrap-secrets.yml   # BWSM access token
ansible-playbook playbooks/bootstrap-flux.yml      # install FluxOperator + FluxInstance

# Flux pulls from GitHub and auto-reconciles everything
```
