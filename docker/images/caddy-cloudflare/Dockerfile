FROM caddy:2.11.7-builder AS builder

# renovate: datasource=go depName=github.com/caddy-dns/cloudflare
ARG CADDY_DNS_CLOUDFLARE_VERSION=v0.2.4
# renovate: datasource=go depName=github.com/hslatman/caddy-crowdsec-bouncer
ARG CADDY_CROWDSEC_BOUNCER_VERSION=v0.14.1
RUN xcaddy build \
	--with github.com/caddy-dns/cloudflare@${CADDY_DNS_CLOUDFLARE_VERSION} \
	--with github.com/hslatman/caddy-crowdsec-bouncer/http@${CADDY_CROWDSEC_BOUNCER_VERSION} \
	--with github.com/hslatman/caddy-crowdsec-bouncer/appsec@${CADDY_CROWDSEC_BOUNCER_VERSION}

FROM caddy:2.11.7
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
