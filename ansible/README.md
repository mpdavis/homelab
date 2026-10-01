# Ansible

Configures the Proxmox hosts and everything inside the guests Tofu creates.
Tofu (`tofu/proxmox`) makes the LXCs and VMs; Ansible does the rest.

## Prerequisites

- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) on your machine
- Root SSH access to every host with `~/.ssh/id_ed25519`
- The collections in `requirements.yml`: `ansible-galaxy collection install -r requirements.yml`
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
| `docker_hosts` | apps, infra | Docker Compose hosts, deployed by doco-cd |
| `gpu` | apps | Holds the passthrough GPU |
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
| `maintain-guests.yml` | every guest | Patches one guest at a time, reboots where an update asks, prunes Docker images |
| `maintain-pve.yml` | `pve` | Patches one node at a time, reboots onto a new kernel and waits for its guests |

From bare Proxmox, in order (doco-cd then deploys every stack from `main`):

```bash
ansible-playbook playbooks/setup-pve.yml
ansible-playbook playbooks/setup-pve-cluster.yml
tofu -chdir=../tofu/proxmox apply
ansible-playbook playbooks/docker-host.yml
```

## Maintenance

Ubuntu's unattended-upgrades already applies security patches to the guests
daily, but it never reboots, and it leaves the NVIDIA driver alone (see
`roles/gpu`). The maintenance playbooks cover the rest:

```bash
ansible-playbook playbooks/maintain-guests.yml
ansible-playbook playbooks/maintain-pve.yml
```

Both work through their hosts one at a time and stop at the first failure.
Run them from the Mac, not devbox: devbox lives on pve1 and reboots during
`maintain-guests.yml`. A node reboot takes its guests with it: pve2 carries the
compose host, and pve1 carries infra, devbox and the subnet router. Gatus will
alert while they're down. Both mark the window with a Grafana annotation,
using a token fetched with `bws`; without `BWS_ACCESS_TOKEN` they skip it.

## Roles

Every guest play starts with `common`; the rest are applied by group.

| Role | Applied to | Purpose |
| --- | --- | --- |
| `pve` | Proxmox hosts | Repos, subscription nag, HA off, NIC offloading, panic/overcommit sysctls, update |
| `common` | every guest | Cloud-init wait, `resolv.conf`, apt cache, base packages, panic/overcommit sysctls on VMs |
| `lxc` | LXC guests | AppArmor removal, `/dev/kmsg` symlink, shared mount for Docker |
| `vm` | VM guests | qemu-guest-agent |
| `gpu` | `gpu` hosts | NVIDIA driver and container toolkit, kept out of unattended-upgrades; reboots once to load a fresh driver |
| `docker` | docker hosts, devbox | Docker Engine, daemon config, shared networks |
| `service_ips` | docker hosts | Extra addresses (`service_ips_addresses`) on the primary interface (netplan drop-in) |
| `doco_cd` | docker hosts | Bitwarden token and doco-cd's first start; doco-cd updates itself after that |
| `tailscale` | tailscale-router, devbox | tailscaled and first `tailscale up` |
| `devbox` | devbox | User, shell, tooling, repos for the development host |
| `apt_upgrade` | maintenance | dist-upgrade, autoremove, autoclean |
| `reboot_required` | maintenance, guests | Reboots when `/var/run/reboot-required` exists |
| `pve_reboot` | maintenance, `pve` | Reboots onto a newer kernel, gated on quorum, then waits for the node's guests |
| `docker_prune` | maintenance | Removes unused images and build cache older than a week |
| `grafana_annotation` | maintenance | Opens and closes a region annotation in Grafana Cloud |
