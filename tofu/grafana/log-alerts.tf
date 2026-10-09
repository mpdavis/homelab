# Alerts on container logs. They live here rather than in grafana-cloud/rules/
# because that pipeline's mimirtool only parses PromQL; it dropped Loki support.
# Routed by the same `severity` label as the synced rules.

resource "grafana_folder" "log_alerts" {
  title = "log-alerts"
}

resource "grafana_rule_group" "mousehole" {
  name             = "mousehole"
  folder_uid       = grafana_folder.log_alerts.uid
  interval_seconds = 300

  # MAM locks a session to a list of ASNs. When the VPN exits through a new
  # one, every update is rejected with "403 Invalid session - ASN mismatch"
  # and stays rejected until the ASN is added to the session in MAM. Matching
  # all rejections also catches an expired or revoked mam_id; "Could not reach
  # MAM" is a network blip that retries on its own, so it is left out.
  rule {
    name      = "MouseholeMamUpdateRejected"
    condition = "C"
    # Updates run every 5 minutes, so this is three rejections in a row.
    for = "10m"
    # No matching lines means no series, which is the healthy state.
    no_data_state  = "OK"
    exec_err_state = "Error"

    labels = {
      severity = "warning"
    }

    annotations = {
      summary     = "MAM is rejecting mousehole's IP updates"
      description = <<-EOT
        mousehole on {{ $labels.host }} has had its MAM dynamic-seedbox updates
        rejected for 10 minutes. "ASN mismatch" means the VPN exit moved to a
        new ASN: add it to the session in MAM (Preferences > Security).
        `docker logs downloads-mousehole-1` has the exact error.
      EOT
    }

    data {
      ref_id         = "A"
      datasource_uid = "grafanacloud-logs"

      relative_time_range {
        from = 900
        to   = 0
      }

      model = jsonencode({
        refId     = "A"
        queryType = "instant"
        expr      = "sum by (host) (count_over_time({service=\"mousehole\"} |= \"MAM update not applied\" [15m]))"
      })
    }

    data {
      ref_id         = "C"
      datasource_uid = "__expr__"

      relative_time_range {
        from = 0
        to   = 0
      }

      model = jsonencode({
        refId      = "C"
        type       = "threshold"
        expression = "A"
        conditions = [{ evaluator = { type = "gt", params = [0] } }]
      })
    }
  }
}
