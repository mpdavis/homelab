variable "zone_id" {
  description = "Cloudflare zone for mpdavis.com"
  type        = string
  default     = "417c69f4a937cb189ff4694ab33b555c"
}

variable "records" {
  description = "Hostname (without domain) to target name."
  type        = map(string)
  default = {
    thumbs         = "public"
    seerr          = "public"
    audiobookshelf = "public"
    council        = "public"
    emby           = "public"
    ntfy           = "public"
    status         = "public"
    iam            = "public"
    home           = "compose_tailnet"
    podfetch       = "compose_tailnet"
    gridiron       = "compose_tailnet"
    dispatcharr    = "compose_tailnet"
    teamarr        = "compose_tailnet"
    ecm            = "compose_tailnet"
    qbit           = "compose_tailnet"
    mousehole      = "compose_tailnet"
    prowlarr       = "compose_tailnet"
    sonarr         = "compose_tailnet"
    radarr         = "compose_tailnet"
    listenarr      = "compose_tailnet"
    paperless      = "compose_tailnet"
    ai             = "compose_tailnet"
    trading        = "compose_tailnet"
    proxmox        = "infra_tailnet"
    birdnet        = "infra_tailnet"
  }
}
