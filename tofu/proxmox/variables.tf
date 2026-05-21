variable "proxmox_endpoint" {
  description = "Proxmox API URL (e.g. https://10.0.1.42:8006)"
  type        = string
}

variable "proxmox_api_token" {
  description = "API token in format: user@realm!tokenid=uuid"
  type        = string
  sensitive   = true
}

variable "proxmox_node" {
  description = "Proxmox node name"
  type        = string
  default     = "pve"
}

variable "proxmox_host" {
  description = "Proxmox host IP (used for SSH operations by the provider)"
  type        = string
}

variable "vm_user" {
  description = "Default user created on VMs via cloud-init"
  type        = string
  default     = "michael"
}

variable "ssh_public_keys" {
  description = "SSH public keys injected into VMs via cloud-init"
  type        = list(string)
}

variable "network_gateway" {
  description = "Network gateway"
  type        = string
  default     = "10.0.0.1"
}

variable "network_cidr" {
  description = "Network CIDR suffix for VM IPs"
  type        = string
  default     = "/16"
}

variable "dns_servers" {
  description = "DNS servers for VMs"
  type        = list(string)
  default     = ["1.1.1.1"]
}

variable "vms" {
  description = "VM definitions — each key becomes the VM hostname"
  type = map(object({
    vmid      = number
    cores     = number
    memory    = number
    disk_size = number
    ip        = string
  }))
  default = {
    k3s-server = {
      vmid      = 200
      cores     = 4
      memory    = 4096
      disk_size = 32
      ip        = "10.0.1.50"
    }
    k3s-agent = {
      vmid      = 201
      cores     = 4
      memory    = 10240
      disk_size = 64
      ip        = "10.0.1.51"
    }
  }
}
