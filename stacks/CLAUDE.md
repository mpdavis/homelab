# Compose stacks

One directory per compose project, discovered automatically by doco-cd — the
project takes the directory's name, and nothing lists it anywhere else. This
tree belongs to the compose host; `infra/` is the same shape for the infra host.

`docs/compose.md` covers the deploy path and the host runbook.

## Exposure

A hostname is served by exactly one Caddy instance, decided by which Caddyfile
it appears in:

| File | Reachable from |
|---|---|
| `stacks/proxy/Caddyfile.tailnet` | LAN and tailnet |
| `stacks/proxy/Caddyfile.public` | the internet |
| `infra/proxy/Caddyfile.tailnet` | LAN and tailnet, for what is not on the compose host |

Default to tailnet. A hostname in two files fails CI, on one host or across
both.

## Routing

Routed services join the external `proxy` network, and Caddy reaches them as
`<service>:<port>`. Nothing publishes a LAN port for Caddy's benefit — the
Ansible `docker` role creates the network, because stacks deploy in parallel
and no stack can be relied on to create it first.

## Secrets

Never in the repo. A stack's `.doco-cd.yml` maps env vars to Bitwarden UUIDs:

```yaml
external_secrets:
  CLOUDFLARE_API_TOKEN: 67c9d80b-...
```

and the compose file refers to each as `${VAR:?resolved by doco-cd from
external_secrets}`, so a missing secret fails loudly instead of starting with an
empty value. A UUID that also appears in the `bws-secret-ids` ConfigMap gets a
comment naming its `BWS_*` key, so the two can be matched until `kubernetes/` is
retired. The host's Bitwarden machine account has to be able to read whatever
its stacks reference.

## Conventions

- **Pin images**, by tag or digest; `image-pin-check` resolves every added
  reference against its registry. First-party images come from `images/` and are
  pinned by digest — see `images/CLAUDE.md`.
- **Don't set `container_name`.** Compose's default names let doco-cd recreate
  a container in place; a fixed name collides during a recreate.
- **Config files are bind-mounted from the stack directory.** doco-cd recreates
  a service when a file it mounts changes, so no hash label or manual restart is
  needed.
- **Healthchecks decide deployment success.** doco-cd waits for them, and
  restarts a container that later goes unhealthy.

## Translating a k3s workload

| In the cluster | In a stack |
|---|---|
| `securityContext.runAsUser/runAsGroup` | `user: "1000:1000"` — keep the same ids or the service loses access to files it owns on NFS |
| `local-path` PVC | a named volume; its data is copied in before first start |
| NFS PVC or inline `nfs:` volume | a volume with `driver_opts` (`type: nfs`, `nfsvers=3`) |
| `${VAR}` from `cluster-vars` | the literal value — there is no postBuild substitution here |
| Service DNS (`x.ns.svc.cluster.local`) | the container name on the `proxy` network, or a LAN address |
| liveness/readiness probe | `healthcheck:` — doco-cd waits on it and restarts on unhealthy |
| `IngressRoute` | a site block in the right Caddyfile |
| Authentik forward-auth middleware | `forward_auth` in the site block |

**The NAS exports are restricted by client IP.** A host that is not on the
allowlist gets `permission denied` at mount time, which surfaces as a failed
deployment rather than anything about permissions in the app. Check with
`showmount -e 10.0.1.6` from the host before moving anything that touches NFS.

## What CI checks

`validate-stacks` renders every compose file, verifies every `external_secrets`
UUID and that each tree has a deploy config, runs `caddy validate` on each
Caddyfile against the image that stack pins, and rejects a hostname served
twice.
