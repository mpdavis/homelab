---
name: add-service
description: >
  Add a new service to this homelab as a Docker Compose stack deployed by doco-cd.
  Use this skill whenever the user wants to add, deploy, install, or set up a new
  application or service — a media app, utility, database, dashboard, monitoring tool,
  or anything else. Also use when the user says things like "deploy X", "set up X",
  "add X to the homelab", or "I want to run X". It covers the stack, the Caddy site,
  Gatus checks, the DNS record, and the homepage tile, so nothing is left unmonitored
  or unreachable.
user-invocable: true
argument-hint: "[service-name]"
---

# Add a service

A service is a compose stack under `stacks/` (compose host, 10.0.1.55) or `infra/`
(infra host, 10.0.1.58). doco-cd polls `main` every 60s and deploys whatever changed, so
merging the PR is the deploy. Read `stacks/CLAUDE.md` first: it holds the authoring rules
this skill does not repeat. `docs/compose.md` is the host runbook.

## 1. Gather the facts

From the project's docs, image page, and repo (use WebFetch; ask the user only for what
you cannot find):

- the image and a **pinned** tag or digest (never `latest`/`edge`)
- the HTTP port, and whether it has its own login
- what it persists (database, config) and whether that belongs on the NAS
- the secrets it needs
- its healthcheck: prefer the one the image ships
  (`docker inspect <image> --format '{{.Config.Healthcheck}}'`)
- whether it needs the GPU
- exposure: **tailnet by default**; public only if people off the tailnet need it

## 2. Pick the stack

Put it in the stack that matches what it does (grouping table in `stacks/CLAUDE.md`), or a
new `stacks/<name>/` if nothing fits. Get this right first time: moving a service later
renames its volumes. Anything that has to keep working while the compose host is down
(monitoring, alerting) goes under `infra/` instead — see `infra/CLAUDE.md`.

## 3. Write the service

In `stacks/<stack>/compose.yaml`, following `stacks/CLAUDE.md`:

- `restart: unless-stopped`, no `container_name`
- `user: "1000:1000"` unless the image drops privileges itself (s6/gosu images)
- a `healthcheck` using a binary the image actually has — doco-cd waits on it and
  restarts the container when it goes unhealthy, so a wrong check becomes a restart loop
- routed services join the external `proxy` network; name services so they cannot
  collide there (prefix generic names like `server` or `web`)
- local state in a named volume; NAS data in a volume with NFS `driver_opts`
  (`nfsvers=3`), mounted with the long syntax and `volume: {nocopy: true}` — without it an
  empty NFS volume fails to start with `lchown ... operation not permitted`
- config files bind-mounted from the stack directory (doco-cd recreates the service when
  they change)
- GPU: a `deploy.resources.reservations.devices` entry with `driver: cdi` and
  `device_ids: [nvidia.com/gpu=all]`
- secrets as `${VAR:?resolved by doco-cd from external_secrets}`, with the Bitwarden UUID
  in the stack's `.doco-cd.yml` under `external_secrets`. Never put secret values in git;
  create the Bitwarden secret first and ask the user for its UUID if you cannot create it.
- only `timeout:` in `.doco-cd.yml` if first start is slow (default 180s)

## 4. Route it

Add a site block to exactly one Caddyfile:

| Exposure | File |
|---|---|
| tailnet (default) | `stacks/proxy/Caddyfile.tailnet` |
| public | `stacks/proxy/Caddyfile.public` |
| tailnet, on the infra host | `infra/proxy/Caddyfile.tailnet` |

```caddy
app.mpdavis.com {
	reverse_proxy app:8080
}
```

With no login of its own, put it behind Authentik's forward auth. If the app has an
unauthenticated health path, serve it ahead of the auth check so Gatus can test the app
itself:

```caddy
app.mpdavis.com {
	route {
		reverse_proxy /health app:8080
		import authentik
		reverse_proxy app:8080
	}
}
```

Apps with native OIDC (like Paperless) skip forward auth and get an Authentik provider
instead — add a blueprint under `stacks/authentik/blueprints/`.

## 5. Monitor it

In `infra/gatus/`:

- `config.yaml`: an endpoint in the right group — `external-open` (`*open-conditions`,
  expects 200) or `external-auth` (`*auth-client`, `*auth-headers`, `*auth-conditions`,
  expects the 302 to Authentik; a 200 means forward auth is missing). With a health path
  served ahead of auth, also an `internal` `<name>-app` check against it.
- `config.yaml`: a `dns-<host>` check under the matching anchor (`*public-dns`,
  `*compose-tailnet-dns`, `*infra-tailnet-dns`).
- `compose.yaml`: an `extra_hosts` line mapping the hostname to the Caddy that serves it
  (10.0.1.56 public, 10.0.1.57 compose tailnet, 10.0.1.58 infra).

## 6. DNS and the homepage

- Add the hostname to `records` in `bootstrap/tofu/cloudflare/variables.tf` with its target
  (`public`, `compose_tailnet`, `infra_tailnet`). The apply is manual and runs from the
  primary checkout (`tofu -chdir=~/git/homelab/bootstrap/tofu/cloudflare apply`), after
  merge — see `bootstrap/tofu/CLAUDE.md`. Tell the user it is pending.
- Add a tile to `stacks/homepage/config/services.yaml` unless it is machine-facing only.

## 7. Check before opening the PR

- `bash .github/scripts/validate-stacks.sh` on a host with Docker (the devbox cannot run
  it; the compose host can): renders every compose file, verifies the Bitwarden UUIDs,
  runs `caddy validate`, and rejects a hostname served twice
- the image reference resolves (`image-pin-check` does this in CI)
- the NAS export allowlist includes the host if it mounts NFS (`showmount -e 10.0.1.6`)

After merge, confirm the deploy in doco-cd's log (`ssh root@10.0.1.55 docker logs
doco-cd-doco-cd-1`), then `curl --resolve <host>:443:<caddy ip> https://<host>/`, and
that its Gatus checks go green once the DNS record is applied.

## Checklist

- [ ] Stack chosen per the grouping in `stacks/CLAUDE.md`; image pinned
- [ ] Healthcheck uses a binary in the image
- [ ] NFS volumes use `nocopy: true`; GPU via CDI
- [ ] Secrets only as Bitwarden UUIDs in `.doco-cd.yml`
- [ ] Site in exactly one Caddyfile, tailnet unless public is needed; forward auth unless
      the app has its own login or OIDC
- [ ] Gatus endpoint, `dns-<host>` check, and `extra_hosts` line
- [ ] `records` entry in `bootstrap/tofu/cloudflare`, apply flagged as pending
- [ ] Homepage tile
