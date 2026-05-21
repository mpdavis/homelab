# Homelab

GitOps repository for a multi-node homelab running **k3s** on **Proxmox VE**, managed by **ArgoCD**.

## Architecture

- **Proxmox VE** hypervisor across two SFF Lenovo nodes
- **k3s** for Kubernetes orchestration
- **ArgoCD** watches this repo and reconciles cluster state
- **External Secrets Operator** syncs secrets from Bitwarden Secrets Manager

See [docs/design.md](docs/design.md) for the full design document, hardware details, storage strategy, and migration plan.

## Repository Layout

```
tofu/           # OpenTofu (IaC) — Proxmox VM provisioning
ansible/        # Ansible — k3s installation and node configuration
apps/           # Per-service K8s manifests (auto-discovered by ArgoCD ApplicationSet)
infra/          # Cluster infrastructure (ArgoCD, cert-manager, MetalLB, etc.)
clusters/       # ArgoCD ApplicationSet definitions
docs/           # Design documents
```

## Getting Started

### Prerequisites

- [OpenTofu](https://opentofu.org/docs/intro/install/) — VM provisioning
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) — node configuration
- [kubectl](https://kubernetes.io/docs/tasks/tools/) — cluster interaction
- SSH access to Proxmox host

### Provision VMs

```bash
cd tofu/proxmox
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your PVE API token and SSH keys
tofu init
tofu apply
```

### Install k3s

```bash
cd ansible
ansible-playbook playbooks/site.yml
```

This installs k3s (server + agent), joins nodes to the cluster, and writes a `kubeconfig.yaml` to the repo root.

### Access the cluster

```bash
export KUBECONFIG=$(pwd)/kubeconfig.yaml
kubectl get nodes
```
