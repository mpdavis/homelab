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

variable "mail_records" {
  description = "Records the outbound mail provider needs to send as notify.mpdavis.com (docker/infra/mail). Keyed by a stable label; name is relative to the zone."
  type = map(object({
    name     = string
    type     = string
    content  = string
    priority = optional(number)
  }))
  # Resend's, from its dashboard (Domains → notify.mpdavis.com). Swapping
  # provider means replacing this map. On a subdomain so the root's MX and SPF
  # stay Google's.
  default = {
    dkim = {
      name    = "resend._domainkey.notify"
      type    = "TXT"
      content = "REPLACE_WITH_RESEND_DKIM_VALUE"
    }
    bounce-mx = {
      name     = "send.notify"
      type     = "MX"
      content  = "feedback-smtp.us-east-1.amazonses.com"
      priority = 10
    }
    bounce-spf = {
      name    = "send.notify"
      type    = "TXT"
      content = "v=spf1 include:amazonses.com ~all"
    }
  }
}
