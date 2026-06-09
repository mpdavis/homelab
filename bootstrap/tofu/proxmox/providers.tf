terraform {
  required_version = ">= 1.5.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.109"
    }
  }
}

provider "proxmox" {
  endpoint = var.proxmox_endpoint
  username = "root@pam"
  password = var.proxmox_password
  insecure = true

  ssh {
    agent = true

    dynamic "node" {
      for_each = var.pve_nodes
      content {
        name    = node.key
        address = node.value.address
      }
    }
  }
}
