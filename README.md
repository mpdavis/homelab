# Homelab

The configuration for my homelab: two Proxmox VE nodes running Docker Compose
stacks, deployed by [doco-cd](https://github.com/kimdre/doco-cd) from this
repository's `main` branch. Merging a change is deploying it.

Service health is public at [status.mpdavis.com](https://status.mpdavis.com).

## What Runs Here

| Area | Services |
| --- | --- |
| Media | Emby (GPU transcoding), Audiobookshelf |
| Downloads | Sonarr, Radarr, Prowlarr, qBittorrent behind Gluetun, Seerr, Recyclarr, Unpackerr, Listenarr, PodFetch |
| Live TV | Dispatcharr, Teamarr, ECM |
| AI | Ollama (GPU) with Open WebUI |
| Documents | Paperless-ngx |
| My own apps | Gridiron, Ames council digest |
| Platform | Caddy, CrowdSec, Authentik, Homepage |
| Monitoring | Gatus, ntfy, Grafana Alloy, node-exporter, cAdvisor |

## How It Fits Together

- **Two Proxmox nodes.** pve2 runs the **compose host**, a VM with the RTX 3050
  passed through, which runs nearly every service. pve1 runs the **infra
  host**, which runs what has to keep working while the compose host is down
  (Gatus, ntfy, and ingress for LAN hosts), plus the Tailscale subnet router
  and a development box.
- **Ingress.** Caddy terminates TLS with Let's Encrypt certificates. A hostname
  is public or tailnet-only depending on which Caddy serves it; the public one
  sits behind CrowdSec. Authentik provides single sign-on, through forward auth
  or native OIDC.
- **Storage.** App state lives in Docker volumes on the hosts' local disks;
  media and bulk data live on a Unifi NAS over NFS.
- **Secrets** live in Bitwarden Secrets Manager, never in git. doco-cd fetches
  them at deploy time.
- **Observability.** Metrics, logs, dashboards and alert rules live in Grafana
  Cloud. Gatus probes every service each minute, and alerts reach my phone
  through ntfy.
- **Updates.** Renovate opens PRs for image and dependency bumps, and CI
  validates every PR before it merges.

[docs/design.md](docs/design.md) explains the reasoning behind these choices.

## Repository Layout

```text
docker/
  stacks/         # compose host stacks, one directory per stack
  infra/          # infra host stacks
  doco-cd/        # doco-cd's deploy config for each host, and doco-cd itself
  images/         # container images built from this repo
ansible/          # host configuration and maintenance, run by hand
tofu/             # OpenTofu: Proxmox guests, Cloudflare DNS, Grafana Cloud alert routing and dashboards
grafana-cloud/    # alert rules, synced to Grafana Cloud on merge
docs/             # design, host runbook, devbox runbook
.github/          # CI workflows and their scripts
```

`AGENTS.md` and the `CLAUDE.md` files are written for coding agents. They also
document the conventions in detail, so they're worth reading too.

## Making Changes

Everything doco-cd deploys lives under `docker/`. Open a PR, let CI validate
it, and merge. Each host's doco-cd polls `main` every minute and applies what
changed. If a merge never shows up, check doco-cd's logs on that host:

```bash
ssh root@<host> docker compose -p doco-cd logs doco-cd
```

A new service needs more than a compose file: a Caddy site, Gatus checks, a DNS
record and a Homepage tile. [docs/compose.md](docs/compose.md#adding-a-service)
lists the steps. Alternatively, open an issue labelled `new service` and CI
drafts the PR.

DNS records (`tofu/cloudflare`) and Grafana routing and dashboards
(`tofu/grafana`) are applied by hand with `tofu apply`. Alert rules in
`grafana-cloud/` sync on merge.

## Building It From Scratch

### Prerequisites

- [OpenTofu](https://opentofu.org/docs/intro/install/) and
  [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/)
- Root SSH access to the Proxmox nodes
- `network.yaml` at the repo root. It's git-ignored and holds the addresses
  Tofu and Ansible share; copy `network.example.yaml` to start
- The Ansible collections: `ansible-galaxy collection install -r ansible/requirements.yml`

### Steps

After a fresh Proxmox VE install on both nodes:

```bash
cd ansible
ansible-playbook playbooks/setup-pve.yml           # repos, nag removal, NIC fix, updates
ansible-playbook playbooks/setup-pve-cluster.yml   # form the Proxmox cluster

cp ../tofu/proxmox/terraform.tfvars.example ../tofu/proxmox/terraform.tfvars  # then fill it in
tofu -chdir=../tofu/proxmox init
tofu -chdir=../tofu/proxmox apply                  # create the VMs and LXCs

ansible-playbook playbooks/docker-host.yml         # Docker, GPU driver, doco-cd (prompts for Bitwarden tokens)
ansible-playbook playbooks/tailscale-router.yml    # subnet router (prompts for an auth key)
ansible-playbook playbooks/devbox.yml              # development host
```

Once doco-cd is running, it deploys every stack from `main` on its own. Apply
`tofu/cloudflare` and `tofu/grafana` for DNS and alert routing.

[docs/compose.md](docs/compose.md) covers bringing up a Docker host in more
detail, and [docs/devbox.md](docs/devbox.md) covers the development host.

## Maintenance

Unattended-upgrades installs security patches on the guests daily, but it never
reboots them. Two playbooks handle full upgrades and reboots, one host at a
time:

```bash
cd ansible
ansible-playbook playbooks/maintain-guests.yml   # upgrade guests, reboot if needed, prune images
ansible-playbook playbooks/maintain-pve.yml      # upgrade Proxmox nodes, reboot onto new kernels
```

Run them from a laptop, not from devbox, because devbox reboots partway
through. [ansible/README.md](ansible/README.md) covers the playbooks and roles.
