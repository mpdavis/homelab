# Homelab

GitOps repository for a multi-node homelab running **k3s** on **Proxmox VE**, managed by **FluxCD**.

## Architecture

- **Proxmox VE** hypervisor across two SFF Lenovo nodes (pve1 + pve2)
- **k3s** for Kubernetes — LXC containers for control plane + general workloads, VM for GPU node
- **FluxCD** (via FluxOperator) watches this repo on GitHub and reconciles cluster state
- **External Secrets Operator** syncs secrets from Bitwarden Secrets Manager

See [docs/design.md](docs/design.md) for the full design document, hardware details, storage strategy, and deploy sequence.

## Deploy Pipeline & Health

Merging to `main` *is* deploying — Flux reconciles the cluster from `main`.
**Gatus** ([status.mpdavis.com](https://status.mpdavis.com)) continuously probes every
service — HTTP status, TLS validity, and that Authentik-protected hosts actually redirect to
the auth portal. Results feed Prometheus; failing endpoints raise the `GatusEndpointDown`
alert.

See [.github/workflows/README.md](.github/workflows/README.md) for the full workflow reference
and [docs/design.md](docs/design.md#deploy-verification--synthetic-monitoring) for the design.

## Repository Layout

```text
bootstrap/            # Pre-Flux provisioning and configuration
  tofu/               # OpenTofu (IaC) — Proxmox LXC/VM provisioning
  ansible/            # Ansible — Proxmox/node setup, k3s install, Flux bootstrap
kubernetes/           # Flux-managed cluster state (sync root)
  apps/               # Per-service K8s manifests
  infrastructure/     # Cluster infrastructure (HelmReleases, HelmRepositories, companion manifests)
    sources/          # HelmRepository definitions
    controllers/      # HelmRelease definitions
  clusters/           # Flux Kustomization entrypoints (infra.yaml, apps.yaml, flux-system/)
docs/                 # Design documents (incl. devbox.md — the dev host runbook)
```

## Getting Started

### Prerequisites

- [OpenTofu](https://opentofu.org/docs/intro/install/) — LXC/VM provisioning
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) — node configuration
- [kubectl](https://kubernetes.io/docs/tasks/tools/) — cluster interaction
- SSH access to Proxmox hosts (pve1, pve2)

### Configure Proxmox Hosts

After a fresh Proxmox VE install on each node:

```bash
cd bootstrap/ansible
ansible-playbook playbooks/setup-pve.yml          # repos, subscription nag, NIC fix, updates
ansible-playbook playbooks/setup-pve-cluster.yml  # form/join the Proxmox cluster
```

### Provision Infrastructure

```bash
cd bootstrap/tofu/proxmox
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your PVE API token and SSH keys
tofu init
tofu apply
```

### Configure Nodes and Install k3s

```bash
cd bootstrap/ansible
ansible-playbook playbooks/site.yml           # install k3s + apply common/lxc/vm/gpu roles
```

### Bootstrap Cluster

```bash
ansible-playbook playbooks/bootstrap-secrets.yml  # BWSM access token
ansible-playbook playbooks/bootstrap-flux.yml     # install FluxOperator + FluxInstance
```

### Provision the Development Host

`devbox` is an always-on LXC for writing code — coding agents run there and
[herdr](https://herdr.dev) attaches over SSH from a laptop or phone. It sits
outside the cluster on purpose; see [docs/devbox.md](docs/devbox.md).

```bash
ansible-playbook playbooks/devbox.yml             # user, sshd, tooling, repos, Tailscale
```

### Access the Cluster

`site.yml` fetches the kubeconfig to the repo root:

```bash
export KUBECONFIG=$(git rev-parse --show-toplevel)/kubeconfig.yaml
kubectl get nodes
```

Flux will automatically reconcile all infrastructure and apps from the repo.
