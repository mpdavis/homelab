variable "zone_id" {
  description = "Cloudflare zone for mpdavis.com"
  type        = string
  default     = "417c69f4a937cb189ff4694ab33b555c"
}

variable "targets" {
  description = "Where a record can point. Public names resolve to the router, which forwards 443 to the compose host's public Caddy; tailnet names resolve to a Caddy IP reachable over the LAN and the Tailscale subnet route."
  type        = map(string)
  default = {
    public          = "46.110.78.19"
    compose_tailnet = "10.0.1.57"
    infra_tailnet   = "10.0.1.58"
  }
}

variable "records" {
  description = "Hostname (without domain) to target name. Only services served by the compose hosts belong here — ExternalDNS still owns the records for whatever is left in k3s, and a hostname moves here as it is cut over."
  type        = map(string)
  default = {
    thumbs   = "public"
    home     = "compose_tailnet"
    podfetch = "compose_tailnet"
    proxmox  = "infra_tailnet"
    birdnet  = "infra_tailnet"
  }
}
