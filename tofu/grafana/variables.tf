variable "ntfy_token" {
  description = "ntfy token with write access to the homelab-alerts topic; set TF_VAR_ntfy_token, see CLAUDE.md."
  type        = string
  sensitive   = true
}

variable "sm_access_token" {
  description = "Synthetic Monitoring API token; set TF_VAR_sm_access_token, see CLAUDE.md."
  type        = string
  sensitive   = true
}

variable "discord_alerts_webhook" {
  description = "Discord webhook URL for the #homelab-alerts forum; set TF_VAR_discord_alerts_webhook, see CLAUDE.md."
  type        = string
  sensitive   = true
}
