# devbox — the always-on development host

`devbox` is an LXC container on pve1 (`10.0.1.54`, vmid 204) that exists to be
the machine your code lives on. Coding agents run there rather than on a laptop,
and [herdr](https://herdr.dev) attaches to it over SSH — from a laptop, a phone,
or anything else that can hold a terminal. Close the lid and the agents keep
working; reattach later and the panes are where you left them.

The rationale for putting it outside the cluster is in
[design.md](design.md#development-host). This file is the runbook.

## Provisioning

Three secrets are needed before you start. All three live in Bitwarden Secrets
Manager alongside the cluster's; the playbook prompts for them and writes
nothing back to this repo.

| Prompt | What it is |
|---|---|
| Tailscale auth key | Reusable, tagged `tag:devbox`, from the Tailscale admin console |
| GitHub PAT | Classic `repo` scope, or fine-grained with Contents + Pull requests RW on every repo in the clone list |
| Claude OAuth token | Output of `claude setup-token` (~1 year expiry); blank to skip |

Then:

```bash
tofu -chdir=bootstrap/tofu/proxmox apply
cd bootstrap/ansible
ansible-playbook -i inventory/hosts.yml playbooks/devbox.yml
```

The first play edits `/etc/pve/lxc/204.conf` on pve1 to pass `/dev/net/tun`
through and reboots the container. This is the one piece Tofu cannot express —
the `bpg/proxmox` provider exposes no device passthrough for containers — so if
Tofu ever destroys and recreates this container, re-run the playbook.

## Tailnet ACL

Tailscale SSH is enabled, and it **denies every connection until the tailnet ACL
allows it**. Add a rule like this in the Tailscale admin console:

```json
{
  "ssh": [
    {
      "action": "accept",
      "src": ["autogroup:member"],
      "dst": ["tag:devbox"],
      "users": ["michael", "autogroup:nonroot"]
    }
  ]
}
```

This is what lets a phone connect with no key material at all — authorisation is
the tailnet identity, not a private key the phone has to store. Ordinary
key-based sshd still listens on the LAN and tailnet IPs; that is the path
Ansible and herdr's own `machine add` use, and it keeps working if you never
touch the ACL.

## Connecting

Once the box is on the tailnet it answers to its MagicDNS name:

```bash
ssh michael@devbox
```

Register it with herdr from your laptop, in an interactive terminal — herdr asks
before it installs anything on the remote:

```bash
herdr machine add devbox --label "homelab devbox"
```

herdr needs its own binary on the remote and installs it on first connect; the
Ansible role also installs it to `~/.local/bin/herdr`, so the box is usable
directly over plain SSH before herdr has ever seen it.

## Working from a phone

herdr's release binaries do not support Android/Termux, so the phone is a client
that SSHes in rather than a host that runs herdr locally. Connect over Tailscale
with any SSH app and run `herdr` on the box. Sessions survive the connection
dropping, which is the point — a phone on cellular will drop.

## Adding a repo

The clone list is `devbox_repos` in
`bootstrap/ansible/roles/devbox/defaults/main.yml`. Add the HTTPS URL and re-run
the playbook; the clone step never touches a repo that already exists, so it
will not move HEAD or discard uncommitted work on a box you have been using.

Some directories in `~/git` on the laptop cannot be cloned from anywhere and are
deliberately absent from the list:

- `ai-scripts`, `homelab-sentinel`, `homelab-starr` — remotes point at
  `git.mpdavis.com` / `git.home.mpdavis.com`, which no longer resolves.
- `homelab-buildarr`, `coloring-pages`, `hubspot`, `hubspot-interview` — no
  remote configured.

Push them to GitHub and add them to the list, or `rsync` them up as one-off
local directories.

## Rotating credentials

Re-run the playbook and supply the new value at the prompt.

- **GitHub PAT** — the playbook only authenticates `gh` when `gh auth status`
  fails, so to force a rotation run `gh auth logout --hostname github.com` on
  the box first.
- **Claude token** — the template is rewritten every run, so a new value at the
  prompt is enough.
- **Tailscale** — `tailscale up` is not safely re-runnable once authenticated.
  Change hostname, tags, routes or SSH with `tailscale set` on the box instead.

## Disk pressure

pve1's LVM thin pool is the constraint, not the container's 40 GB. Check it on
pve1:

```bash
lvs pve/data
```

The container's disk is thin-provisioned, so only written blocks are charged,
but a pool that actually fills can wedge every guest on pve1 — including the k3s
nodes. If `Data%` climbs past ~90%, reclaim space in this order:

1. `docker system prune -a` — build caches are usually the biggest and the
   cheapest to lose.
2. Point `docker_data_root` (in `roles/docker/defaults/main.yml`) at an
   NFS-backed path. Slower and it loses hardlink semantics, so do this second.
3. Move model weights and `~/.cache` to the `homelab` NFS share.

## Troubleshooting

**`tailscale up` fails or the box never appears on the tailnet.** Check TUN
passthrough survived: `grep dev/net /etc/pve/lxc/204.conf` on pve1 should show
both the cgroup allow rule and the mount entry. Re-run the playbook if not.

**Docker will not start.** Nesting must be on (`nesting = true` in the Tofu
container definition) and `/` must be rshared — the `lxc` role's `rc.local`
handles the latter on boot.

**SSH works but Tailscale SSH is refused.** That is the ACL, not the box. The
`ssh` block above has to name `tag:devbox` as a destination.

**Agents cannot push.** `gh auth status` on the box, as the `michael` user. If
the token expired, rotate it as above.
