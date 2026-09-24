resource "cloudflare_dns_record" "service" {
  for_each = var.records

  zone_id = var.zone_id
  name    = "${each.key}.mpdavis.com"
  type    = "A"
  content = var.targets[each.value]
  # 1 is Cloudflare's "auto" TTL.
  ttl     = 1
  proxied = false
  comment = "Managed by OpenTofu (${each.value})"
}
