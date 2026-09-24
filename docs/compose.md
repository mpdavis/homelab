# Docker Compose hosts

The homelab is moving from k3s + Flux to Docker Compose, one namespace-shaped stack
at a time. During the migration both run side by side: `kubernetes/` stays
authoritative for everything that has not been cut over, and `stacks/` holds what
has.

The target split is pve2 as the Compose host — it holds the GPU, and
`k3s-agent-gpu` already carries most of the load — and pve1 as the
infrastructure host for things that should not depend on it.

## Layout

```text
doco-cd/
  .doco-cd.yaml           # what doco-cd deploys: everything in stacks/
  compose.yaml            # the doco-cd instance itself (applied by Ansible)
stacks/
  <stack>/                # one compose project per stack, auto-discovered
    compose.yaml
    .doco-cd.yml          # optional: per-stack settings, e.g. external_secrets
    ...                   # config files the stack bind-mounts
```

Everything runs on one host: the `docker` VM (205) on pve2, `10.0.1.55`. A second
host would reintroduce a per-host split — doco-cd selects a deploy config by poll
target, so it would become `.doco-cd.<host>.yaml` with a `stacks/<host>/` tree.

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

The proxy stack runs beside the apps on the compose host, so Caddy reaches them
by container name and no stack publishes a LAN port.

A second Caddy is planned for the infra host (pve1), owning the routes that have
nothing to do with compose: Proxmox (`10.0.1.1:8006`), BirdNET
(`10.0.63.190:80`), and whatever else lands there later. Both are LAN endpoints
it can reach directly, so neither Caddy ever proxies through the other — each
hostname's DNS record points at whichever one owns it.

That instance stays tailnet-only, and **every public hostname terminates on the
compose host's Caddy**: the router forwards 443 to exactly one IP, so public
routes cannot be split across two instances.

## Conventions

- **Exposure is set by which Caddyfile a site is in.**
  - `Caddyfile.tailnet` is served on `10.0.1.57`, which is never port-forwarded,
    so those sites are reachable only from the LAN and the tailnet.
  - `Caddyfile.public` will be served on `10.0.1.56`, which becomes the router's
    443 forward target at cutover.

  A hostname in both files fails `validate-stacks`. Default new services to
  tailnet.
- **Routing goes over the shared `proxy` network.** A routed service joins the
  external `proxy` network, and Caddy reaches it as `<service>:<port>`. The
  `docker` Ansible role creates it (`docker_networks` in the inventory), because
  doco-cd deploys stacks in parallel and no single stack can be relied on to
  create it first.
- **Secrets are never written into the repo.** A stack's `.doco-cd.yml` maps
  environment variables to Bitwarden secret UUIDs under `external_secrets`. The
  compose file refers to each one as `${VAR:?resolved by doco-cd from
  external_secrets}`, so a missing secret fails loudly instead of starting with an
  empty value.
  - A UUID that is also in the `bws-secret-ids` ConfigMap gets a comment naming
    its `BWS_*` key, so the two can be matched until `kubernetes/` is retired.
  - The host's Bitwarden machine account must be able to read every secret its
    stacks use.
- **Pin image tags.** `image-pin-check` resolves every added `image:` under
  `stacks/` and `doco-cd/`, and Renovate bumps them through its docker-compose
  manager.
- **Don't set `container_name`.** Compose's default names let doco-cd recreate
  a container in place. A fixed name collides during a recreate.

## CI

- `validate-stacks.yml` (advisory):
  - renders every compose file under `stacks/` and `doco-cd/`;
  - checks every `external_secrets` UUID;
  - checks the deploy config still discovers `stacks/`;
  - builds the proxy image and runs `caddy validate` on each Caddyfile;
  - rejects hostnames that appear in more than one Caddyfile.
- `image-pin-check.yml` (required): now covers `stacks/**` and `doco-cd/**`.

## Bringing up a host

1. Create the VM: `tofu -chdir=bootstrap/tofu/proxmox apply`. Only `docker`
   should be new.
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
   Bitwarden token at the prompt.
4. Check the result:
   - `docker ps` on the host shows `doco-cd` and the stacks. The stacks appear
     within a minute or two of doco-cd starting.
   - `curl --resolve home.mpdavis.com:443:10.0.1.57 https://home.mpdavis.com/`
     returns the page.

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
