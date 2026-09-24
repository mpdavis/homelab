# Docker Compose hosts

The homelab is moving from k3s + Flux to Docker Compose, one namespace-shaped stack
at a time. During the migration both run side by side: `kubernetes/` stays
authoritative for everything that has not been cut over, and `stacks/` holds what
has.

The target split is pve2 as the Compose host — it holds the GPU, and
`k3s-agent-gpu` already carries most of the load — and pve1 as the
infrastructure host for things that should not depend on it.

This page is the runbook: how a change reaches a host, how to bring one up, and
how to cut a service over. The rules for writing a stack — exposure, routing,
secrets, pinning — live in `stacks/CLAUDE.md`.

## Layout

```text
doco-cd/
  .doco-cd.yaml           # what the compose host deploys: everything in stacks/
  .doco-cd.infra.yaml     # what the infra host deploys: everything in infra/
  compose.yaml            # the doco-cd instance itself (applied by Ansible)
stacks/                   # the compose host's projects
  <stack>/                # one compose project per stack, auto-discovered
    compose.yaml
    .doco-cd.yml          # optional: per-stack settings, e.g. external_secrets
    ...                   # config files the stack bind-mounts
infra/                    # the infra host's projects, same shape
  <stack>/
```

| Host     | Where                       | Runs                                     |
| -------- | --------------------------- | ---------------------------------------- |
| `docker` | VM 205 on pve2, `10.0.1.55` | the migrated services + their Caddies    |
| `infra`  | VM 206 on pve1, `10.0.1.58` | Caddy for what is not on the docker host |

Each host runs one doco-cd, told which tree to deploy by its poll target: the
compose host uses the default config, the infra host sets `DOCO_TARGET=infra`
(`doco_cd_target` in the inventory). A third host would add a tree and a
`.doco-cd.<target>.yaml`.

## How a change deploys

[doco-cd](https://doco.cd) runs on the host and polls `main` every 60 seconds.
It fills the role Flux has today. When a commit touches a stack, doco-cd:

- resolves the stack's `external_secrets` from Bitwarden;
- runs the compose up, building any `build:` images;
- recreates services whose bind-mounted files changed.

Deleting a stack's directory removes the project. Its volumes are kept.
doco-cd also restarts containers that turn unhealthy, which covers what
Kubernetes liveness probes did.

**doco-cd does not deploy itself.** Recreating its own container mid-deploy would
kill the deploy, so the `doco_cd` Ansible role owns `doco-cd/compose.yaml`:
re-running the playbook applies a version bump or any other change to it.
Renovate bumps the pinned image there like any other. Handing that apply to a
GitHub Action, or to a second doco-cd instance that only deploys the first, is
open.

Deploy failures are visible in `docker logs -f doco-cd-doco-cd-1` for now. There
is no push alert: the obvious sources both want something that hasn't moved yet
— an Apprise sidecar needs an ntfy token, which only a second instance could
resolve, and doco-cd's Prometheus metrics (port 9120) want the monitoring stack
on this host, after which the existing Alertmanager route to ntfy covers it.

## Where Caddy runs

Three instances, one per (host, exposure). All build the same image; only the
Caddyfile and the IP differ.

| Instance              | Host   | IP           | Serves                                             |
| --------------------- | ------ | ------------ | -------------------------------------------------- |
| `proxy/caddy-tailnet` | docker | `10.0.1.57`  | migrated services, by container name               |
| `proxy/caddy-public`  | docker | `10.0.1.56`  | the same, once any of them is public               |
| `proxy/caddy-tailnet` | infra  | `10.0.1.58`  | Proxmox, BirdNET — LAN endpoints, reached directly |

No instance proxies through another: each hostname's DNS record points at the IP
that serves it. `validate-stacks` rejects a hostname that appears in two
Caddyfiles, on one host or across both.

**Every public hostname terminates on the compose host's `caddy-public`.** The
router forwards 443 to exactly one IP, so public routes cannot be split. That
forward still points at Traefik's VIP and moves to `10.0.1.56` when the first
public service is cut over — until then `Caddyfile.public` has no sites, which
is valid and serves nothing.

## Bringing up a host

1. Create the VM: `tofu -chdir=bootstrap/tofu/proxmox apply`. Note the state and
   `terraform.tfvars` live only in the primary checkout, not in worktrees.
2. Give the host a Bitwarden access token **(manual)**. Machine accounts are
   granted per project, and every secret lives in the single `homelab` project,
   so any token that can read the Cloudflare API token can read all of them —
   a second machine account buys independent rotation and a distinguishable
   audit trail, not narrower access. Create one for the host anyway, so
   revoking it doesn't also break External Secrets; reusing the ESO token
   (`secret/bitwarden-access-token` in `external-secrets`) works if you prefer
   one credential. Narrower access would mean splitting the project.
3. Run the playbook:
   `ansible-playbook -i inventory/hosts.yml playbooks/docker-host.yml`. Paste the
   Bitwarden token at the prompt. Add `--limit <host>` to do one host.
4. Check the result:
   - `docker ps` on the host shows `doco-cd` and the stacks. The stacks appear
     within a minute or two of doco-cd starting.
   - `curl --resolve <hostname>:443:<caddy ip> https://<hostname>/` returns the
     page, before DNS points anywhere near it.

doco-cd polls `main`, so provision after this branch merges. If you provision
first, it logs "config not found" until the merge lands, then converges on its
own.

## Cutting a service over

The Kubernetes copy keeps serving until DNS moves.

1. Add the stack under `stacks/`, and its hostname to the right Caddyfile.
   Merge, then test through the new proxy with `curl --resolve`.
2. Stop the Kubernetes workload. Copy its data from
   `/var/lib/rancher/k3s/storage/pvc-*` on the node that holds it, or from the NFS
   share. Stateless services such as homepage skip this step.
3. Remove it from `kubernetes/`, and move its Gatus `hostAliases` entry to the
   Caddy IP. With `policy: sync`, ExternalDNS deletes the old A record once the
   IngressRoute is gone.
4. Recreate the record pointing at the Caddy IP. Wait until ExternalDNS has
   removed it first: until then ExternalDNS owns the record through its TXT
   registry and would reset it. Moving these records into OpenTofu is an open
   decision.
