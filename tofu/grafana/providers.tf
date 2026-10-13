terraform {
  required_version = ">= 1.5.0"

  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.46"
    }
  }
}

# Reads GRAFANA_AUTH (a service account token) from the environment; see
# CLAUDE.md.
provider "grafana" {
  url             = "https://luckyzeppelin876.grafana.net"
  sm_url          = "https://synthetic-monitoring-api-us-east-0.grafana.net"
  sm_access_token = var.sm_access_token
}
