# Grafana IRM: every alert becomes an alert group, and each alert group gets a
# post in the Discord #homelab-alerts forum, where Hob investigates it
# (docs/sre-agent.md). No escalation chain yet, so IRM pages nobody; ntfy still
# does.

resource "grafana_oncall_integration" "homelab" {
  name = "Homelab alerts"
  type = "grafana_alerting"

  default_route {}
}

# Discord returns the created message only with wait=true, and that message's
# channel_id is the new forum post. IRM keeps the response per alert group, so
# the status webhook below can reply inside the same post.
resource "grafana_oncall_outgoing_webhook" "discord_open" {
  name               = "Discord: open post"
  url                = "${var.discord_alerts_webhook}?wait=true"
  trigger_type       = "alert group created"
  integration_filter = [grafana_oncall_integration.homelab.id]
  headers            = jsonencode({ "Content-Type" = "application/json" })

  # Hob finds the alert group by the `IRM alert group:` line; keep its format.
  data = <<-EOT
    {
      "thread_name": {{ alert_group.title[:100] | tojson }},
      "content": {{ (
        (alert_payload.commonAnnotations.summary or alert_payload.commonAnnotations.description or "")[:1500]
        ~ "\n\nIRM alert group: `" ~ alert_group.id ~ "`"
        ~ "\n<" ~ alert_group.permalinks.web ~ ">"
      ) | tojson }},
      "allowed_mentions": {"parse": []}
    }
  EOT
}

resource "grafana_oncall_outgoing_webhook" "discord_status" {
  name               = "Discord: status change"
  url                = "${var.discord_alerts_webhook}?thread_id={{ responses[\"${grafana_oncall_outgoing_webhook.discord_open.id}\"].channel_id }}"
  trigger_type       = "status change"
  integration_filter = [grafana_oncall_integration.homelab.id]
  headers            = jsonencode({ "Content-Type" = "application/json" })

  data = <<-EOT
    {
      "content": {{ {
        "resolve": "✅ Resolved",
        "acknowledge": "👀 Acknowledged",
        "unacknowledge": "Unacknowledged",
        "unresolve": "🔥 Firing again",
        "silence": "🔕 Silenced",
        "unsilence": "🔔 Unsilenced"
      }.get(event.type, event.type) | tojson }},
      "allowed_mentions": {"parse": []}
    }
  EOT
}
