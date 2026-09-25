output "container_ips" {
  description = "IP addresses of provisioned LXC containers"
  value = {
    for name, c in var.containers : name => local.net.hosts[name]
  }
}

output "vm_ips" {
  description = "IP addresses of provisioned VMs"
  value = {
    for name, v in var.vms : name => local.net.hosts[name]
  }
}
