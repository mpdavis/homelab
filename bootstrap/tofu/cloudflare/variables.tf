variable "zone_id" {
  description = "Cloudflare zone for mpdavis.com"
  type        = string
  default     = "417c69f4a937cb189ff4694ab33b555c"
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
