variable "proxmox_password" {
  description = "Password for root@pam (or set PROXMOX_VE_PASSWORD env var)"
  type        = string
  sensitive   = true
  default     = null
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

variable "containers" {
  description = "LXC container definitions"
  type = map(object({
    vmid        = number
    node        = string
    cores       = number
    memory      = number
    disk_size   = number
    privileged  = bool
    nesting     = bool
    keyctl      = bool
    tun         = optional(bool, false)
    start_order = optional(number, 0)
    tags        = optional(list(string), [])
  }))
  default = {
    tailscale-router = {
      vmid      = 203
      node      = "pve1"
      cores     = 1
      memory    = 512
      disk_size = 8
      # Privileged dates from when TUN had to be passed through with raw LXC
      # config lines. device_passthrough (`tun`) works unprivileged too, but
      # flipping it recreates the container.
      #
      # Nesting: required even though nothing here runs nested containers.
      # This template's systemd (255) fails most units — including
      # systemd-networkd — with "Failed to set up mount namespacing:
      # Permission denied" (exit 226/NAMESPACE) without it. Confirmed by
      # direct testing: the container came up with no network at all until
      # this was flipped to true. Proxmox warns about this at apply time.
      privileged  = true
      nesting     = true
      keyctl      = false
      tun         = true
      start_order = 3
      tags        = ["tailscale", "subnet-router"]
    }
    devbox = {
      vmid   = 204
      node   = "pve1"
      cores  = 4
      memory = 8192
      # Thin-provisioned, but pve1's pool is 141G at ~69%. Watch `lvs pve/data`.
      disk_size = 40
      # Privileged for the same historical TUN reason as tailscale-router
      # above; nesting is also required for Docker. A privileged LXC running coding agents is a
      # deliberate trade — treat a devbox compromise as a pve1 compromise.
      privileged  = true
      nesting     = true
      keyctl      = true
      tun         = true
      start_order = 4
      tags        = ["devbox", "development"]
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
    gpu_mapping = optional(string)
    tags        = optional(list(string), [])
  }))
  default = {
    # The Docker Compose host, and pve2's only guest. See docs/compose.md. A VM
    # with a passthrough device locks all of its RAM up front, so memory beyond
    # what the host (62G) needs for itself is never shared back.
    docker = {
      vmid        = 205
      node        = "pve2"
      cores       = 4
      memory      = 40960
      disk_size   = 128
      gpu_mapping = "gpu"
      tags        = ["docker", "gpu"]
    }
    # Ingress for what does not run on the compose host, so those routes
    # survive pve2 maintenance. A VM, not an LXC: Docker in an LXC breaks on
    # runc/AppArmor (see the devbox).
    infra = {
      vmid      = 206
      node      = "pve1"
      cores     = 1
      memory    = 1024
      disk_size = 8
      tags      = ["docker", "infra"]
    }
  }
}
