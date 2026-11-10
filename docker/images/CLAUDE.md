# Images

Container images built from this repo, one directory per image, published to
`ghcr.io/mpdavis/<name>` by `.github/workflows/publish-images.yml` on every push
to `main` that touches `docker/images/`.

## When an image belongs here

When it is a build recipe with no source of its own — a Dockerfile and
nothing else — or when its code exists only to run this homelab. Anything with
a life of its own (gridiron) gets its own `mpdavis/<name>` repo and publishes
from there.

`hob` is here because it is the homelab's SRE agent (`docs/sre-agent.md`): its
code, its stack and the repo it maintains change together.

`caddy-cloudflare` is here because the stock Caddy image ships no DNS providers,
and DNS-01 is the only ACME challenge that works for hosts the internet cannot
reach. Both proxy stacks need it, and the infra host is too small to run an
xcaddy build itself.

## Consuming an image

Stacks pin by digest:

```yaml
image: ghcr.io/mpdavis/caddy-cloudflare:latest@sha256:<digest>
```

A Dockerfile change here then reaches the hosts as an ordinary Renovate digest
PR, which `image-pin-check` verifies resolves before it can merge. Pinning by
tag alone would leave the hosts on whatever `:latest` happened to be, and a
plugin-only bump would not change any tag at all.

Take the digest from the publish workflow's job summary.

## Gotchas

- A package published by `GITHUB_TOKEN` can start out private. The hosts and
  `image-pin-check` both pull anonymously, so a new image needs its package set
  to public once, or every pin fails to resolve.
- Pin the upstream tag in `FROM` and keep a `# renovate:` annotation above any
  version `ARG`; neither is tracked otherwise.
