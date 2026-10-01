# Dashboards live here as JSON, so a UI edit is overwritten on the next apply.
# Export a UI change with "Copy JSON" and commit it rather than saving in place.

resource "grafana_folder" "homelab" {
  title = "Homelab"
}

resource "grafana_dashboard" "crowdsec" {
  folder      = grafana_folder.homelab.uid
  config_json = file("${path.module}/dashboards/crowdsec.json")
}
