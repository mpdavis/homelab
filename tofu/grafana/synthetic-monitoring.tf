# Grafana Cloud's probes check the homelab from the internet. Gatus can't:
# it resolves every hostname to a Caddy LAN address, so it never crosses the
# WAN, the router's 443 forward or the public IP. The alert on this check is
# in grafana-cloud/rules/synthetic-monitoring.yml.

data "grafana_synthetic_monitoring_probes" "main" {}

# status.mpdavis.com because it is public and unauthenticated: a 200 means the
# whole public path (router, caddy-public, CrowdSec) answered, ending at Gatus
# on the infra host.
resource "grafana_synthetic_monitoring_check" "public_ingress" {
  job     = "public-ingress"
  target  = "https://status.mpdavis.com"
  enabled = true
  # Where Synthetic Monitoring files every check; left unset, each plan
  # tries to clear it.
  folder_uid = "grafana-synthetic-monitoring-app"
  # Three probes, so one probe's own network trouble is outvoted. Every two
  # minutes keeps 3 probes inside the free tier's 100k executions a month.
  probes = [
    data.grafana_synthetic_monitoring_probes.main.probes.Ohio,
    data.grafana_synthetic_monitoring_probes.main.probes.NorthVirginia,
    data.grafana_synthetic_monitoring_probes.main.probes.Oregon,
  ]
  frequency = 120000
  timeout   = 10000
  labels    = {}

  settings {
    http {
      ip_version         = "V4"
      method             = "GET"
      fail_if_not_ssl    = true
      valid_status_codes = [200]
    }
  }
}
