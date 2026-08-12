# Ansible

Ansible playbooks and roles for provisioning the homelab infrastructure.

## Prerequisites

- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) installed on your local machine
- SSH access to all target hosts (key-based, configured in `inventory/hosts.yml`)
- For PVE playbooks: root access to Proxmox hosts
- For k3s playbooks: a user with sudo privileges on k3s nodes

## Inventory

Hosts are defined in `inventory/hosts.yml` and organized into groups:

| Group | Hosts | Purpose |
|-------|-------|---------|
| `pve` | pve1, pve2 | Proxmox VE hypervisors |
| `k3s_server` | k3s-server | k3s control plane |
| `k3s_agent` | k3s-agent-1, k3s-agent-gpu | k3s worker nodes |
| `k3s_cluster` | (all k3s nodes) | Parent group for k3s |

## Playbooks

Run playbooks from the `ansible/` directory:

```bash
ansible-playbook playbooks/<playbook>.yml
```

### Proxmox Host Setup

Run these after a fresh Proxmox VE install on each node.

| Playbook | Target | Description |
|----------|--------|-------------|
| `setup-pve.yml` | `pve` | Post-install config: repos (no-subscription), subscription nag removal, NIC offloading fix, system update |
| `setup-pve-cluster.yml` | `pve` | Creates a Proxmox cluster on the first node and joins all others. Safe to re-run when adding new nodes. |

### Node Configuration

Run this after Tofu has provisioned the LXC containers and VMs.

| Playbook | Target | Description |
|----------|--------|-------------|
| `site.yml` | `k3s_cluster` | Installs k3s (server + agents), applies common/lxc/vm/gpu roles as needed. |

### Cluster Bootstrap

Run these after k3s is installed and nodes have joined.

| Playbook | Target | Description |
|----------|--------|-------------|
| `bootstrap-secrets.yml` | `k3s_server` | Creates the Bitwarden Secrets Manager access token secret. Prompts for the token. |
| `bootstrap-flux.yml` | `k3s_server` | Installs FluxOperator via Helm and applies the FluxInstance CR. Flux then reconciles everything else from the repo. |

## Full Deploy Order

```bash
# 1. Configure Proxmox hosts (after fresh PVE install)
ansible-playbook playbooks/setup-pve.yml
ansible-playbook playbooks/setup-pve-cluster.yml

# 2. Provision LXC/VMs with Tofu (not Ansible)
# cd ../tofu/proxmox && tofu apply

# 3. Configure nodes and install k3s
ansible-playbook playbooks/site.yml

# 4. Bootstrap cluster services
ansible-playbook playbooks/bootstrap-secrets.yml
ansible-playbook playbooks/bootstrap-flux.yml
```

## Roles

| Role | Applied to | Purpose |
|------|-----------|---------|
| `pve` | Proxmox hosts | Repo config, subscription nag, NIC offloading, system update |
| `common` | All k3s nodes | Cloud-init wait, apt cache, base packages |
| `lxc` | LXC nodes | `/dev/kmsg` symlink, shared mount for k3s |
| `vm` | VM nodes | qemu-guest-agent |
| `gpu` | k3s-agent-gpu | NVIDIA drivers, container toolkit, containerd config |
| `k3s_server` | k3s-server | k3s server install |
| `k3s_agent` | k3s agents | k3s agent install and cluster join |
