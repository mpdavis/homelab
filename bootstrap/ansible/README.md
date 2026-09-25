# Ansible

Configures the Proxmox hosts and everything inside the guests Tofu creates.
Tofu (`bootstrap/tofu/proxmox`) makes the LXCs and VMs; Ansible does the rest.

## Prerequisites

- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) on your machine
- Root SSH access to every host with `~/.ssh/id_ed25519`
- `bootstrap/network.yaml` (see below)

## Inventory

Hosts are defined in `inventory/hosts.yml` without addresses. Every IP comes
from `bootstrap/network.yaml` (git-ignored; copy `bootstrap/network.example.yaml`),
which `inventory/group_vars/all/network.yml` symlinks in as the `network` var.
`inventory/network.yml`, a `constructed` inventory source, turns it into each
host's `ansible_host`, so `ansible.cfg` points at the whole `inventory/`
directory — don't pass `-i inventory/hosts.yml`. Tofu reads the same file. From
a worktree, symlink the primary checkout's copy in first.

| Group | Hosts | Purpose |
| --- | --- | --- |
| `pve` | pve1, pve2 | Proxmox VE hypervisors |
| `k3s_cluster` | `k3s_server` + `k3s_agent` | k3s-server; k3s-agent-1, k3s-agent-gpu |
| `docker_hosts` | docker, infra | Docker Compose hosts, deployed by doco-cd |
| `tailscale_router` | tailscale-router | Tailscale subnet router for the LAN |
| `development` | devbox | Always-on development host |
| `lxc` / `vm` | every guest, by type | Sets `node_type`; target platform roles with e.g. `k3s_cluster:&vm` |

## Playbooks

Run from `bootstrap/ansible/`: `ansible-playbook playbooks/<playbook>.yml`.

| Playbook | Target | What it does |
| --- | --- | --- |
| `setup-pve.yml` | `pve` | Post-install config: no-subscription repos, nag removal, NIC offload fix, sysctls, dist-upgrade |
| `setup-pve-cluster.yml` | `pve` | Creates the Proxmox cluster on the first node and joins the rest; safe to re-run |
| `site.yml` | `k3s_cluster` | Prepares the nodes and installs k3s (server, then agents) |
| `bootstrap-secrets.yml` | `k3s_server` | Creates the Bitwarden access token secret for External Secrets (prompts) |
| `bootstrap-flux.yml` | `k3s_server` | Installs the Flux Operator and applies the FluxInstance |
| `docker-host.yml` | `docker_hosts` | Docker, service IPs, and the doco-cd instance; see `docs/compose.md` |
| `tailscale-router.yml` | `tailscale_router` | The subnet router (prompts for an auth key) |
| `devbox.yml` | `development` | The development host; see `docs/devbox.md` |

A fresh cluster, in order:

```bash
ansible-playbook playbooks/setup-pve.yml
ansible-playbook playbooks/setup-pve-cluster.yml
tofu -chdir=../tofu/proxmox apply
ansible-playbook playbooks/site.yml
ansible-playbook playbooks/bootstrap-secrets.yml
ansible-playbook playbooks/bootstrap-flux.yml
```

## Roles

Every guest play starts with `common`; the rest are applied by group.

| Role | Applied to | Purpose |
| --- | --- | --- |
| `pve` | Proxmox hosts | Repos, subscription nag, HA off, NIC offloading, k3s sysctls, update |
| `common` | every guest | Cloud-init wait, `resolv.conf`, apt cache, base packages, k3s sysctls on VMs |
| `lxc` | LXC guests | AppArmor removal, `/dev/kmsg` symlink, shared mount for k3s and Docker |
| `vm` | VM guests | qemu-guest-agent |
| `gpu` | k3s-agent-gpu | NVIDIA driver, container toolkit, containerd runtime |
| `k3s_server` | k3s-server | k3s server install, kubeconfig fetch |
| `k3s_agent` | k3s agents | k3s agent install and cluster join |
| `docker` | docker hosts, devbox | Docker Engine, daemon config, shared networks |
| `service_ips` | docker hosts | Extra addresses on the primary interface (netplan drop-in) |
| `doco_cd` | docker hosts | Bitwarden token and the doco-cd instance |
| `tailscale` | tailscale-router, devbox | tailscaled and first `tailscale up` |
| `devbox` | devbox | User, shell, tooling, repos for the development host |
