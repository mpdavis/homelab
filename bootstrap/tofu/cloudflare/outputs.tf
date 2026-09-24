output "records" {
  description = "Hostname to address, as published"
  value       = { for name, r in cloudflare_dns_record.service : r.name => r.content }
}
