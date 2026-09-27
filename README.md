# Homelab

GitOps repository for a two-node homelab on **Proxmox VE**. Services run as
**Docker Compose** stacks, deployed by **doco-cd** from `main`.

## Architecture

- **Proxmox VE** on two SFF Lenovo nodes (pve1 + pve2)
- **Compose host** (VM on pve2) runs every service, with the RTX 3050 passed
  through for Emby transcoding and Ollama
- **Infra host** (VM on pve1) runs what must survive the compose host being
  down: Gatus, ntfy, and the Caddy for LAN hosts
- **Caddy** terminates TLS, one instance per host and exposure (public or
  tailnet); **Authentik** provides SSO
- **Bitwarden Secrets Manager** holds every secret; doco-cd resolves them at
  deploy time
- **Grafana Cloud** holds metrics, logs and alert rules; alerts arrive through
  ntfy

See [docs/design.md](docs/design.md) for the design and
[docs/compose.md](docs/compose.md) for the runbook.

## Deploy Pipeline & Health

Merging to `main` is deploying: each host's doco-cd polls `main` every minute
and applies what changed. **Gatus** ([status.mpdavis.com](https://status.mpdavis.com))
probes every service — HTTP status, TLS validity, and that Authentik-protected
hosts actually redirect to the login page — and failing checks alert through
Grafana Cloud.

See [.github/workflows/README.md](.github/workflows/README.md) for CI.

## Repository Layout

```text
stacks/               # the compose host's projects, one directory per stack
infra/                # the infra host's projects
doco-cd/              # doco-cd config per host + the doco-cd instance itself
images/               # container images built from this repo
grafana-cloud/        # alert rules, synced to Grafana Cloud on merge
bootstrap/
  tofu/               # OpenTofu — Proxmox guests, Cloudflare DNS, alert routing
  ansible/            # Ansible — Proxmox and guest configuration, doco-cd
docs/                 # design, compose runbook, devbox runbook
```

## Getting Started

### Prerequisites

- [OpenTofu](https://opentofu.org/docs/intro/install/) — guests and DNS
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) — host configuration
- SSH access to the Proxmox hosts (pve1, pve2)
- `bootstrap/network.yaml` (git-ignored; `network.example.yaml` shows its shape)

### Configure Proxmox Hosts

After a fresh Proxmox VE install on each node:

```bash
cd bootstrap/ansible
ansible-playbook playbooks/setup-pve.yml          # repos, subscription nag, NIC fix, updates
ansible-playbook playbooks/setup-pve-cluster.yml  # form/join the Proxmox cluster
```

### Provision the Guests

```bash
cd bootstrap/tofu/proxmox
cp terraform.tfvars.example terraform.tfvars      # SSH keys, Proxmox password
tofu init && tofu apply
```

### Configure the Docker Hosts

```bash
cd bootstrap/ansible
ansible-playbook playbooks/docker-host.yml        # Docker, networks, GPU driver, doco-cd
```

The playbook prompts for each host's Bitwarden access token. Once doco-cd is
running it deploys every stack from `main` on its own; DNS records are applied
from `bootstrap/tofu/cloudflare`.

### Provision the Development Host

`devbox` is an always-on LXC for writing code — coding agents run there and
[herdr](https://herdr.dev) attaches over SSH from a laptop or phone. See
[docs/devbox.md](docs/devbox.md).

```bash
ansible-playbook playbooks/devbox.yml             # user, sshd, tooling, repos, Tailscale
```
