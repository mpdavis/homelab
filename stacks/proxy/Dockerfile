# The stock image has no DNS providers; DNS-01 is the only ACME challenge that
# works for tailnet-only hosts, which the internet can't reach for HTTP-01.
FROM caddy:2.11.4-builder AS builder

# renovate: datasource=go depName=github.com/caddy-dns/cloudflare
ARG CADDY_DNS_CLOUDFLARE_VERSION=v0.2.4
RUN xcaddy build --with github.com/caddy-dns/cloudflare@${CADDY_DNS_CLOUDFLARE_VERSION}

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
