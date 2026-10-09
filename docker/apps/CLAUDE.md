# Compose stacks

One directory per compose project, discovered automatically by doco-cd — the
project takes the directory's name, and nothing lists it anywhere else. This
tree belongs to the compose host; `docker/infra/` is the same shape for the infra host.

Services are grouped by what they do, not one stack each:

| Stack | Holds |
|---|---|
| `media` | what serves a library: emby, audiobookshelf |
| `downloads` | what acquires: qbittorrent (with gluetun, mousehole and pf-watchdog), prowlarr, sonarr, radarr, unpackerr, recyclarr, podfetch, listenarr, seerr |
| `iptv` | dispatcharr, teamarr, ecm, game-thumbs |
| `ai` | ollama, open-webui |
| `trading` | QuantDinger: its API, workers, frontend, and their Postgres, Redis and Kafka |
| `authentik` | the identity provider every forward-auth site and OIDC app uses |
| `docs`, `civic`, `gridiron`, `homepage` | one app each |
| `proxy` | the host's Caddy instances, and CrowdSec watching the public one |
| `monitoring` | the host's Grafana Cloud agent |

Put a service in the right stack the first time. Moving it later renames the
compose project, which renames its volumes — so the data has to be copied
again, with the service stopped.

`docs/compose.md` covers the deploy path and the host runbook.

## Exposure

A hostname is served by exactly one Caddy instance, decided by which Caddyfile
it appears in:

| File | Reachable from |
|---|---|
| `docker/apps/proxy/Caddyfile.tailnet` | LAN and tailnet |
| `docker/apps/proxy/Caddyfile.public` | the internet |
| `docker/infra/proxy/Caddyfile.tailnet` | LAN and tailnet, for what is not on the compose host |

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
empty value. The host's Bitwarden machine account has to be able to read whatever
its stacks reference.

## Conventions

- **Pin images**, by tag or digest; `image-pin-check` resolves every added
  reference against its registry. First-party images come from `docker/images/`
  and are pinned by digest — see `docker/images/CLAUDE.md`.
- **Don't set `container_name`.** Compose's default names let doco-cd recreate
  a container in place; a fixed name collides during a recreate.
- **Config files are bind-mounted from the stack directory.** doco-cd recreates
  a service when a file it mounts changes, so no hash label or manual restart is
  needed.
- **Healthchecks decide deployment success.** doco-cd waits for them, and
  restarts a container that later goes unhealthy — so a check that is subtly
  wrong becomes a restart loop. Prefer the one the image ships
  (`docker inspect <image> --format '{{.Config.Healthcheck}}'`) over writing
  one, and if you do write one, use a binary the image actually has.
- **Volumes are named `<project>_<key>`,** so the stack a service lives in
  decides its volume names. Data copied in by hand has to go to that name.
- **Service names are global on the `proxy` network.** A generic name (`web`,
  `server`, `redis`) collides with another stack's; prefix it, or keep the
  service off `proxy` if only its own stack talks to it.
- **Forward auth** is `import authentik` inside a `route` block, ahead of the
  app's `reverse_proxy`. An unauthenticated health path goes before the import,
  so Gatus can check the app itself (see `prowlarr` in `Caddyfile.tailnet`).
- **Scheduled jobs** use the image's own scheduler if it has one (recyclarr's
  `CRON_SCHEDULE`), else a service looping `run; sleep` — sleeping after each
  run means runs can never overlap (see `docker/apps/civic`).
- **The GPU** is requested through CDI: a `deploy.resources.reservations.devices`
  entry with `driver: cdi` and `device_ids: [nvidia.com/gpu=all]`. Only the
  compose host has one.

## NFS

**Mount NFS volumes with `nocopy: true`** (long volume syntax) wherever the
image has files at the mount path. When the volume is empty, Docker seeds it
from the image and chowns the result, the NAS refuses the chown, and the
container fails to start with `lchown ... operation not permitted`.

**The NAS exports are restricted by client IP.** A host that is not on the
allowlist gets `permission denied` at mount time, which surfaces as a failed
deployment rather than anything about permissions in the app. Check with
`showmount -e 10.0.1.6` from the host before adding anything that touches NFS.

## What CI checks

`validate-stacks` renders every compose file, verifies every `external_secrets`
UUID and that each tree has a deploy config, runs `caddy validate` on each
Caddyfile against the image that stack pins, and rejects a hostname served
twice.
