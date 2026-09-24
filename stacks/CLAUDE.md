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

## What CI checks

`validate-stacks` renders every compose file, verifies every `external_secrets`
UUID and that each tree has a deploy config, runs `caddy validate` on each
Caddyfile against the image that stack pins, and rejects a hostname served
twice.
