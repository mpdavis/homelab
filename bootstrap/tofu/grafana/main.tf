# Where the Grafana Cloud stack's alerts go. The rules themselves are in
# grafana-cloud/rules/ and are synced by CI; they reach these contact points
# through the notification policy below, by their severity label.

locals {
  # ntfy's tpl=1 renders the webhook body into the title and message. Same
  # template as the k3s Alertmanager route: Grafana's webhook body has the
  # same status/commonLabels/alerts fields as Alertmanager's.
  ntfy_url = "https://ntfy.mpdavis.com/homelab-alerts?tpl=1&title=%7B%7Bif%20eq%20.status%20%22resolved%22%7D%7D%E2%9C%85%20%7B%7Belse%7D%7D%F0%9F%94%A5%20%7B%7Bend%7D%7D%7B%7B.commonLabels.alertname%7D%7D&message=%7B%7Brange%20.alerts%7D%7D%7B%7B.annotations.description%7D%7D%0A%7B%7Bend%7D%7D"

  ntfy_levels = {
    warning  = { priority = 3, tags = "warning" }
    critical = { priority = 5, tags = "rotating_light" }
  }
}

resource "grafana_contact_point" "ntfy" {
  for_each = local.ntfy_levels

  name = "ntfy-${each.key}"

  webhook {
    url                       = "${local.ntfy_url}&priority=${each.value.priority}&tags=${each.value.tags}"
    authorization_scheme      = "Bearer"
    authorization_credentials = var.ntfy_token
  }
}

# Replaces the stack's whole policy tree; nothing else routes alerts here.
resource "grafana_notification_policy" "root" {
  contact_point   = grafana_contact_point.ntfy["warning"].name
  group_by        = ["alertname", "host"]
  group_wait      = "30s"
  group_interval  = "5m"
  repeat_interval = "4h"

  policy {
    matcher {
      label = "severity"
      match = "="
      value = "critical"
    }
    contact_point = grafana_contact_point.ntfy["critical"].name
  }
}
