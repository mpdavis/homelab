# Ansible

Configures the Proxmox hosts and everything inside the guests Tofu creates.
Tofu (`tofu/proxmox`) makes the LXCs and VMs; Ansible does the rest.

## Prerequisites

- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) on your machine
- Root SSH access to every host with `~/.ssh/id_ed25519`
- `network.yaml` (see below)

## Inventory

Hosts are defined in `inventory/hosts.yml` without addresses. Every IP comes
from `network.yaml` (git-ignored; copy `network.example.yaml`),
which `inventory/group_vars/all/network.yml` reads in as the `network` var. It
reads rather than symlinks: doco-cd's clone has no `network.yaml`, and a
dangling symlink fails its every deploy.
`inventory/network.yml`, a `constructed` inventory source, turns it into each
host's `ansible_host`, so `ansible.cfg` points at the whole `inventory/`
directory — don't pass `-i inventory/hosts.yml`. Tofu reads the same file. From
a worktree, symlink the primary checkout's copy in first.

| Group | Hosts | Purpose |
| --- | --- | --- |
| `pve` | pve1, pve2 | Proxmox VE hypervisors |
| `docker_hosts` | docker, infra | Docker Compose hosts, deployed by doco-cd |
| `gpu` | docker | Holds the passthrough GPU |
| `tailscale_router` | tailscale-router | Tailscale subnet router for the LAN |
| `development` | devbox | Always-on development host |
| `lxc` / `vm` | every guest, by type | Sets `node_type`; target platform roles with e.g. `docker_hosts:&vm` |

## Playbooks

Run from `ansible/`: `ansible-playbook playbooks/<playbook>.yml`.

| Playbook | Target | What it does |
| --- | --- | --- |
| `setup-pve.yml` | `pve` | Post-install config: no-subscription repos, nag removal, NIC offload fix, sysctls, dist-upgrade |
| `setup-pve-cluster.yml` | `pve` | Creates the Proxmox cluster on the first node and joins the rest; safe to re-run |
| `docker-host.yml` | `docker_hosts` | Docker, service IPs, and the doco-cd instance; see `docs/compose.md` |
| `tailscale-router.yml` | `tailscale_router` | The subnet router (prompts for an auth key) |
| `devbox.yml` | `development` | The development host; see `docs/devbox.md` |

From bare Proxmox, in order (doco-cd then deploys every stack from `main`):

```bash
ansible-playbook playbooks/setup-pve.yml
ansible-playbook playbooks/setup-pve-cluster.yml
tofu -chdir=../tofu/proxmox apply
ansible-playbook playbooks/docker-host.yml
```

## Roles

Every guest play starts with `common`; the rest are applied by group.

| Role | Applied to | Purpose |
| --- | --- | --- |
| `pve` | Proxmox hosts | Repos, subscription nag, HA off, NIC offloading, panic/overcommit sysctls, update |
| `common` | every guest | Cloud-init wait, `resolv.conf`, apt cache, base packages, panic/overcommit sysctls on VMs |
| `lxc` | LXC guests | AppArmor removal, `/dev/kmsg` symlink, shared mount for Docker |
| `vm` | VM guests | qemu-guest-agent |
| `gpu` | `gpu` hosts | NVIDIA driver and container toolkit; reboots once to load a fresh driver |
| `docker` | docker hosts, devbox | Docker Engine, daemon config, shared networks |
| `service_ips` | docker hosts | Extra addresses on the primary interface (netplan drop-in) |
| `doco_cd` | docker hosts | Bitwarden token and the doco-cd instance |
| `tailscale` | tailscale-router, devbox | tailscaled and first `tailscale up` |
| `devbox` | devbox | User, shell, tooling, repos for the development host |
