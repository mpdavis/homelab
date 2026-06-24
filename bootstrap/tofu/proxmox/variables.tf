variable "proxmox_endpoint" {
  description = "Proxmox API URL (e.g. https://10.0.1.1:8006)"
  type        = string
}

variable "proxmox_password" {
  description = "Password for root@pam (or set PROXMOX_VE_PASSWORD env var)"
  type        = string
  sensitive   = true
  default     = null
}

variable "pve_nodes" {
  description = "Proxmox VE nodes — name and SSH address for each"
  type = map(object({
    address = string
  }))
  default = {
    pve1 = { address = "10.0.1.1" }
    pve2 = { address = "10.0.1.2" }
  }
}

variable "vm_user" {
  description = "Default user created on VMs/containers via cloud-init"
  type        = string
  default     = "root"
}

variable "ssh_public_keys" {
  description = "SSH public keys injected into VMs/containers via cloud-init"
  type        = list(string)
}

variable "network_gateway" {
  description = "Network gateway"
  type        = string
  default     = "10.0.0.1"
}

variable "network_cidr" {
  description = "Network CIDR suffix for IPs"
  type        = string
  default     = "/16"
}

variable "dns_servers" {
  description = "DNS servers"
  type        = list(string)
  default     = ["1.1.1.1"]
}

variable "containers" {
  description = "LXC container definitions"
  type = map(object({
    vmid         = number
    node         = string
    cores        = number
    memory       = number
    disk_size    = number
    ip           = string
    privileged   = bool
    nesting      = bool
    keyctl       = bool
    start_order  = optional(number, 0)
    tags         = optional(list(string), [])
  }))
  default = {
    k3s-server = {
      vmid       = 200
      node       = "pve1"
      cores      = 4
      memory     = 8192
      disk_size  = 32
      ip         = "10.0.1.50"
      privileged = true
      nesting    = true
      keyctl     = true
      start_order = 1
      tags       = ["k3s", "server"]
    }
    k3s-agent-1 = {
      vmid       = 201
      node       = "pve1"
      cores      = 4
      memory     = 16384
      disk_size  = 64
      ip         = "10.0.1.51"
      privileged = true
      nesting    = true
      keyctl     = true
      start_order = 2
      tags       = ["k3s", "agent"]
    }

  }
}

variable "vms" {
  description = "VM definitions (for nodes requiring full VM, e.g. GPU passthrough)"
  type = map(object({
    vmid        = number
    node        = string
    cores       = number
    memory      = number
    disk_size   = number
    ip          = string
    gpu_mapping = optional(string)
    tags        = optional(list(string), [])
  }))
  default = {
    k3s-agent-gpu = {
      vmid        = 202
      node        = "pve2"
      cores       = 6
      memory      = 49152
      disk_size   = 96
      ip          = "10.0.1.52"
      gpu_mapping = "gpu"
      tags        = ["k3s", "agent", "gpu"]
    }
  }
}
