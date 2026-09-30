variable "ntfy_token" {
  description = "ntfy token with write access to the homelab-alerts topic (BWS_NTFY_ALERTMANAGER_TOKEN); set TF_VAR_ntfy_token, see CLAUDE.md."
  type        = string
  sensitive   = true
}
