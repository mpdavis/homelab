output "vm_ips" {
  description = "IP addresses of provisioned VMs"
  value = {
    for name, vm in proxmox_virtual_environment_vm.k3s : name => var.vms[name].ip
  }
}
