locals {
  net = yamldecode(file("${path.module}/../../network.yaml")).network

  # Where a record can point. Public names resolve to the router, which
  # forwards 443 to the compose host's public Caddy; tailnet names resolve to a
  # Caddy IP reachable over the LAN and the Tailscale subnet route.
  targets = {
    public          = local.net.public_ip
    compose_tailnet = local.net.service_ips.compose_tailnet
    infra_tailnet   = local.net.hosts.infra
  }
}

resource "cloudflare_dns_record" "service" {
  for_each = var.records

  zone_id = var.zone_id
  name    = "${each.key}.mpdavis.com"
  type    = "A"
  content = local.targets[each.value]
  # 1 is Cloudflare's "auto" TTL.
  ttl     = 1
  proxied = false
  comment = "Managed by OpenTofu (${each.value})"
}
