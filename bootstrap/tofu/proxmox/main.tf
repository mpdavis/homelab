locals {
  container_nodes = toset([for c in var.containers : c.node])
  vm_nodes        = toset([for v in var.vms : v.node])
}

# --- LXC Template Downloads ---

resource "proxmox_download_file" "lxc_template" {
  for_each = local.container_nodes

  content_type = "vztmpl"
  datastore_id = "local"
  node_name    = each.key
  url          = "https://mirrors.servercentral.com/ubuntu-cloud-images/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64-root.tar.xz"
}

# --- VM Cloud Image Downloads ---

resource "proxmox_download_file" "vm_cloud_image" {
  for_each = local.vm_nodes

  content_type = "import"
  datastore_id = "local"
  node_name    = each.key
  url          = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
  file_name    = "noble-server-cloudimg-amd64.qcow2"
}

# --- LXC Containers ---

resource "proxmox_virtual_environment_container" "container" {
  for_each = var.containers

  description = "Managed by OpenTofu"
  node_name   = each.value.node
  vm_id       = each.value.vmid
  tags        = each.value.tags

  unprivileged  = !each.value.privileged
  start_on_boot = true

  features {
    nesting = each.value.nesting
    keyctl  = each.value.keyctl
  }

  cpu {
    cores = each.value.cores
  }

  memory {
    dedicated = each.value.memory
  }

  disk {
    datastore_id = "local-lvm"
    size         = each.value.disk_size
  }

  network_interface {
    name = "eth0"
  }

  initialization {
    hostname = each.key

    ip_config {
      ipv4 {
        address = "${each.value.ip}${var.network_cidr}"
        gateway = var.network_gateway
      }
    }

    dns {
      domain  = ""
      servers = var.dns_servers
    }

    user_account {
      keys = var.ssh_public_keys
    }
  }

  operating_system {
    template_file_id = proxmox_download_file.lxc_template[each.value.node].id
    type             = "ubuntu"
  }

  startup {
    order = each.value.start_order
  }
}

# --- VMs (GPU nodes) ---

resource "proxmox_virtual_environment_vm" "vm" {
  for_each = var.vms

  name      = each.key
  node_name = each.value.node
  vm_id     = each.value.vmid
  tags      = each.value.tags

  stop_on_destroy = true
  started         = true
  machine         = "q35"
  bios            = "ovmf"

  cpu {
    cores = each.value.cores
    type  = "host"
  }

  efi_disk {
    datastore_id = "local-lvm"
    type         = "4m"
  }

  # floating = dedicated keeps the balloon device for memory stats but prevents reclaim — AI inference (Ollama) needs stable memory
  memory {
    dedicated = each.value.memory
    floating  = each.value.memory
  }

  agent {
    enabled = true
  }

  initialization {
    ip_config {
      ipv4 {
        address = "${each.value.ip}${var.network_cidr}"
        gateway = var.network_gateway
      }
    }

    dns {
      domain  = ""
      servers = var.dns_servers
    }

    user_account {
      username = var.vm_user
      keys     = var.ssh_public_keys
    }
  }

  disk {
    datastore_id = "local-lvm"
    import_from  = proxmox_download_file.vm_cloud_image[each.value.node].id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = each.value.disk_size
  }

  network_device {
    bridge = "vmbr0"
  }

  operating_system {
    type = "l26"
  }

  dynamic "hostpci" {
    for_each = each.value.gpu_mapping != null ? [each.value.gpu_mapping] : []
    content {
      device  = "hostpci0"
      mapping = hostpci.value
      pcie    = true
      rombar  = true
      xvga    = true
    }
  }
}
