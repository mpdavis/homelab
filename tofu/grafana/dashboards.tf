# Dashboards live here as JSON, so a UI edit is overwritten on the next apply.
# Export a UI change with "Copy JSON" and commit it rather than saving in place.

resource "grafana_folder" "homelab" {
  title = "Homelab"
}

resource "grafana_dashboard" "crowdsec" {
  folder      = grafana_folder.homelab.uid
  config_json = file("${path.module}/dashboards/crowdsec.json")
}

resource "grafana_dashboard" "overview" {
  folder      = grafana_folder.homelab.uid
  config_json = file("${path.module}/dashboards/overview.json")
}

resource "grafana_dashboard" "home" {
  folder      = grafana_folder.homelab.uid
  config_json = file("${path.module}/dashboards/home.json")
}

resource "grafana_organization_preferences" "main" {
  home_dashboard_uid = grafana_dashboard.home.uid
}
