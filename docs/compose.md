# Docker Compose hosts

Everything runs as Docker Compose projects on two VMs: pve2's compose host,
which holds the GPU and nearly every service, and pve1's infra host, for what
has to keep working while the compose host is down.

This page is the runbook: how a change reaches a host, how to bring one up, and
what adding a service touches. The rules for writing a stack — exposure,
routing, secrets, pinning — live in `stacks/CLAUDE.md` and `infra/CLAUDE.md`.

## Layout

```text
doco-cd/
  .doco-cd.yaml           # what the compose host deploys: everything in stacks/
  .doco-cd.infra.yaml     # what the infra host deploys: everything in infra/
  compose.yaml            # the doco-cd instance itself, which deploys it too
stacks/                   # the compose host's projects
  <stack>/                # one compose project per stack, auto-discovered
    compose.yaml
    .doco-cd.yml          # optional: per-stack settings, e.g. external_secrets
    ...                   # config files the stack bind-mounts
infra/                    # the infra host's projects, same shape
  <stack>/
```

| Host     | Where                       | Runs                                             |
| -------- | --------------------------- | ------------------------------------------------ |
| `docker` | VM 205 on pve2, `10.0.1.55` | every service, its two Caddies, the GPU          |
| `infra`  | VM 206 on pve1, `10.0.1.58` | Gatus, ntfy, Caddy for LAN hosts (Proxmox, BirdNET) |

Each host runs one doco-cd, told which tree to deploy by its poll target: the
compose host uses the default config, the infra host sets `DOCO_TARGET=infra`
(`doco_cd_target` in the inventory for the first start, `environment` in
`.doco-cd.infra.yaml` after that). A third host would add a tree and a
`.doco-cd.<target>.yaml`.

## How a change deploys

[doco-cd](https://doco.cd) runs on the host and polls `main` every 60 seconds.
When a commit touches a stack, doco-cd:

- resolves the stack's `external_secrets` from Bitwarden;
- runs the compose up, building any `build:` images;
- recreates services whose bind-mounted files changed.

Deleting a stack's directory removes the project. Its volumes are kept.
doco-cd also restarts containers that turn unhealthy.

**doco-cd deploys itself.** Each host's deploy config lists a `doco-cd`
deployment for `doco-cd/compose.yaml`, and `SELF_UPDATE_ENABLED` lets it
replace its own container
([Self-Updating](https://doco.cd/latest/Advanced/Self-Updating/)), so a
Renovate bump is a merge like any other. It starts the new container beside
the old one and hands over only once the new one is healthy. A version that
never turns healthy is discarded, and that commit is not retried; the next
commit is. The container's number grows with each handover (`doco-cd-2`,
`-3`, …), so reach it by project, not by name.

Self-update refuses a change to the `doco-cd_data` volume, since a rollback
could not restore it, so that needs doing by hand. Recreating doco-cd's
project network falls back to a stop-and-replace handover, a short outage.

The `doco_cd` Ansible role only starts the first instance, and skips a host
where doco-cd already manages itself, so re-running the playbook never rolls
back what doco-cd deployed. A bad release that doco-cd cannot replace on its
own: `docker compose -p doco-cd down` on the host, then run the playbook
from a checkout that pins a working version.

A failed deploy raises `DocoCdDeploymentFailed` in ntfy, from doco-cd's
metrics (see Monitoring below); `docker compose -p doco-cd logs -f doco-cd` on
the host has the reason.

## Monitoring

Metrics, logs and alerting live in a free Grafana Cloud stack — nothing
monitoring-related runs here except an agent and Gatus. Off-site means a dead
host, a dead pve1 or a dead homelab still alerts, which nothing on the LAN can
do about itself.

Every Docker host runs the same agent — Alloy, node-exporter and cAdvisor, in
`infra/monitoring/` and `stacks/monitoring/` — which pushes:

- every container's logs, labelled `host`, `stack`, `service`, `container`;
- host metrics (`job="node"`), per-container metrics (`job="cadvisor"`) and
  doco-cd's own (`job="doco-cd"`), labelled `host`;
- metrics from any container labelled `homelab.metrics.port`, reached on the
  `proxy` network — today only Gatus, trimmed to
  `gatus_results_endpoint_success`.

A new stack needs nothing to be monitored. Alloy's config is identical on
every host — only `HOST_LABEL` differs — and is copied per tree because a
stack can only mount its own files; `validate-stacks` fails if the copies
drift. A new host copies the agent stack, sets `HOST_LABEL`, and adds a
`HostAgentAbsent` clause for itself.

**The free tier caps active series at 10k** and keeps 14 days. The agent keeps
metrics deliberately: container veths are excluded from node-exporter, and
cAdvisor is cut to the metrics a dashboard uses. Check usage in the stack's
cost-management page before adding a scrape.

Alert rules are Prometheus-format files in `grafana-cloud/rules/`.
`grafana-cloud.yml` validates them on PRs and, on merge, syncs them into the
stack as Grafana-managed rules, so change them here rather than in the UI.
Where they notify — the `homelab-alerts` ntfy topic, routed on `severity` — is
`tofu/grafana`, applied by hand like the Cloudflare records.

Going back to self-hosting is a change of the agent's endpoints plus an
Alertmanager config: the agent and rules are standard Prometheus/Loki formats.

## Where Caddy runs

Three instances, one per (host, exposure). All build the same image; only the
Caddyfile and the IP differ.

| Instance              | Host   | IP           | Serves                                             |
| --------------------- | ------ | ------------ | -------------------------------------------------- |
| `proxy/caddy-tailnet` | docker | `10.0.1.57`  | tailnet services, by container name                |
| `proxy/caddy-public`  | docker | `10.0.1.56`  | public services, by container name                 |
| `proxy/caddy-tailnet` | infra  | `10.0.1.58`  | Proxmox, BirdNET — LAN endpoints, reached directly |

Each hostname's DNS record points at the IP that serves it. `validate-stacks` rejects a hostname that appears in two
Caddyfiles, on one host or across both.

**Every public hostname terminates on the compose host's `caddy-public`,** which
the router forwards 443 to. Public routes cannot be split, because the router
forwards to exactly one IP. The infra host's two public services, ntfy and the
Gatus status page, publish a port on `10.0.1.58` that `caddy-public` proxies
to — the only case of one host's Caddy reaching another's service.

## Bringing up a host

1. Create the VM: add its address to `network.yaml`, then
   `tofu -chdir=tofu/proxmox apply`. Note the state, `terraform.tfvars`
   and `network.yaml` live only in the primary checkout, not in worktrees.
2. Give the host a Bitwarden access token **(manual)**. Machine accounts are
   granted per project, and every secret lives in the single `homelab` project,
   so any token that can read the Cloudflare API token can read all of them —
   a second machine account buys independent rotation and a distinguishable
   audit trail, not narrower access. Narrower access would mean splitting the
   project.
3. Run the playbook:
   `ansible-playbook playbooks/docker-host.yml`. Paste the
   Bitwarden token at the prompt. Add `--limit <host>` to do one host.
4. Check the result:
   - `docker ps` on the host shows `doco-cd` and the stacks. The stacks appear
     within a minute or two of doco-cd starting.
   - `curl --resolve <hostname>:443:<caddy ip> https://<hostname>/` returns the
     page, before DNS points anywhere near it.

doco-cd polls `main`, so provision after the host's tree and doco-cd config are
merged. If you provision first, it logs "config not found" until the merge
lands, then converges on its own.

The NAS exports are restricted by client IP: add a new host in the Unifi NAS UI
before it mounts anything, or the mount fails with `permission denied`
(`showmount -e 10.0.1.6` from the host shows the allowlist).

## Adding a service

A new hostname touches four places besides its stack:

1. A site block in the right Caddyfile — see `stacks/CLAUDE.md` for which one,
   and `import authentik` inside a `route` block to put it behind login.
2. A Gatus endpoint in `infra/gatus/config.yaml`, plus an `extra_hosts` line in
   `infra/gatus/compose.yaml` pointing the hostname at the Caddy that serves it.
3. A DNS record: the hostname in `records` in `tofu/cloudflare`, then
   `tofu apply` from the primary checkout.
4. A matching `dns-<host>` Gatus check — the HTTP probe resolves through
   `extra_hosts`, so without it a missing record goes unnoticed.

Test before DNS points anywhere with
`curl --resolve <host>:443:<caddy ip> https://<host>/`.

A stateful service that moves between stacks gets new volume names
(`<project>_<key>`), so stop it, copy the data into the new volumes, and check
it before merging — doco-cd starts the stack within a minute of the merge.
