# Homelab

GitOps repository for a multi-node homelab running **k3s** on **Proxmox VE**, managed by **FluxCD**.

## Architecture

- **Proxmox VE** hypervisor across two SFF Lenovo nodes (pve1 + pve2)
- **k3s** for Kubernetes — LXC containers for control plane + general workloads, VM for GPU node
- **FluxCD** (via FluxOperator) watches this repo on GitHub and reconciles cluster state
- **External Secrets Operator** syncs secrets from Bitwarden Secrets Manager

See [docs/design.md](docs/design.md) for the full design document, hardware details, storage strategy, and deploy sequence.

## Repository Layout

```
tofu/               # OpenTofu (IaC) — Proxmox LXC/VM provisioning
ansible/            # Ansible — k3s installation, node configuration, Flux bootstrap
apps/               # Per-service K8s manifests
infrastructure/     # Cluster infrastructure (HelmReleases, HelmRepositories, companion manifests)
  sources/          # HelmRepository definitions
clusters/           # Flux Kustomization entrypoints (infra.yaml, apps.yaml, flux-system/)
docs/               # Design documents
```

## Getting Started

### Prerequisites

- [OpenTofu](https://opentofu.org/docs/intro/install/) — LXC/VM provisioning
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) — node configuration
- [kubectl](https://kubernetes.io/docs/tasks/tools/) — cluster interaction
- SSH access to Proxmox hosts (pve1, pve2)

### Provision Infrastructure

```bash
cd tofu/proxmox
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your PVE API token and SSH keys
tofu init
tofu apply
```

### Configure Nodes and Install k3s

```bash
cd ansible
ansible-playbook playbooks/site.yml           # install k3s on all nodes
```

### Bootstrap Cluster

```bash
ansible-playbook playbooks/bootstrap-secrets.yml  # BWSM access token
ansible-playbook playbooks/bootstrap-flux.yml     # install FluxOperator + FluxInstance
```

### Access the Cluster

```bash
export KUBECONFIG=$(pwd)/kubeconfig.yaml
kubectl get nodes
```

Flux will automatically reconcile all infrastructure and apps from the repo.
