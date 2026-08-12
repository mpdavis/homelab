output "container_ips" {
  description = "IP addresses of provisioned LXC containers"
  value = {
    for name, c in var.containers : name => c.ip
  }
}

output "vm_ips" {
  description = "IP addresses of provisioned VMs"
  value = {
    for name, v in var.vms : name => v.ip
  }
}
