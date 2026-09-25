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

**Every public hostname terminates on the compose host's `caddy-public`,** which
the router forwards 443 to. Public routes cannot be split, because the router
forwards to exactly one IP.

Public services still in k3s are listed in `Caddyfile.public` with
`import traefik`, which proxies them on to Traefik's VIP unchanged. Migrating
one is then a one-line swap to `reverse_proxy <container>:<port>` — no router
change, no DNS change, and nothing else moves with it.

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

### First, check what the service drags with it

- **Is it public?** No router work is needed — the forward already points at
  the public Caddy — but the swap happens in `Caddyfile.public`, from
  `import traefik` to a `reverse_proxy` at the container.
- **Does it mount NFS?** The host must be on the NAS export allowlist
  (`showmount -e 10.0.1.6`), or the mount fails with `permission denied`.
- **Does anything in the cluster talk to it?** A consumer using
  `x.ns.svc.cluster.local` stops resolving the moment the Service is gone.
- **Does it share files with another service?** Two copies writing the same
  share is worse than downtime; stop one before starting the other.

Test with `curl --resolve <host>:443:<caddy ip> https://<host>/`. Use port 443 —
a different port puts `:port` in the `Host` header, which stops Traefik matching
and looks like a routing bug that isn't.

Never move the router's 443 forward before the Caddyfile that serves those
hostnames is merged: the target answers nothing until doco-cd has deployed it,
and every public service is down in the meantime.

### Stateless

1. Add the stack, and the hostname to the right Caddyfile. Merge, then test with
   `curl --resolve <host>:443:<caddy ip> https://<host>/` before touching DNS.
2. Remove it from `kubernetes/` and move its Gatus `hostAliases` entry to the
   Caddy IP.
3. Once ExternalDNS has deleted the record, add the hostname to `records` in
   `bootstrap/tofu/cloudflare` and apply. Earlier just gets it reset —
   ExternalDNS owns the record through its TXT registry until the IngressRoute
   is gone.

### Stateful

The data has to be copied while nothing is writing it, and the stack must not
start before the data is in place — doco-cd deploys within a minute of the
merge, so the copy happens **before** it, not after:

1. Open the PR (stack added, `kubernetes/` copy removed) but do not merge.
2. Stop the cluster copy. Suspend first, or Flux scales it straight back up:

   ```sh
   flux suspend helmrelease <name> -n <ns>
   kubectl -n <ns> scale deploy <name> --replicas=0
   ```

   A suspended HelmRelease is pruned on merge but never uninstalled, so its
   Deployment and Service outlive it. Clean up afterwards with
   `helm -n <ns> uninstall <name>`; `helm list` is how you spot the leftovers.

3. Copy the data into the target volume, and check it landed:

   ```sh
   docker volume create <project>_<volume>
   ssh <k3s node> 'cd /var/lib/rancher/k3s/storage/pvc-*_<ns>_<name> && tar cf - .' \
     | ssh <compose host> 'tar xf - -C /var/lib/docker/volumes/<project>_<volume>/_data'
   ```

   Then `chown` to the uid the container runs as, and compare checksums. An
   embedded database keeps a WAL — copy it and its sidecar files, not just the
   `.db`.

   **Verify before merging, not after.** Flux prunes the PVC too, and
   local-path deletes the directory with it, so once the merge lands the copy on
   the compose host is the only copy.
4. Merge. Flux prunes the cluster copy; doco-cd starts the stack on the data.
5. DNS as above.
