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
  url = "https://luckyzeppelin876.grafana.net"
}
