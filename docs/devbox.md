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
ansible-playbook playbooks/devbox.yml
```

The first play edits `/etc/pve/lxc/204.conf` on pve1 to pass `/dev/net/tun`
through and reboots the container. This is the one piece Tofu cannot express —
the `bpg/proxmox` provider exposes no device passthrough for containers — so if
Tofu ever destroys and recreates this container, re-run the playbook.

## Tailnet ACL

Two separate things are needed in the tailnet policy file, and the first one
bites before the second ever matters.

**1. `tag:devbox` must be owned.** A tagged auth key is rejected outright if
nothing owns the tag, so the playbook fails at `tailscale up` — long before any
SSH rule is consulted:

```json
{
  "tagOwners": {
    "tag:devbox": ["autogroup:admin"]
  }
}
```

**2. Tailscale SSH denies every connection until an `ssh` rule allows it.** This
is its own top-level section — not a grant, and not covered by `grants`/`acls`:

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

Use `"action": "accept"`, not `"check"`. Check mode forces a browser
re-authentication per connection, which is meaningful friction from a phone.

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

## Shell

zsh with oh-my-zsh and the spaceship prompt, installed by the role and
configured from templates — `~/.zshrc` and `~/.aliases` are managed files and
local edits are overwritten.

This is a deliberate subset of the laptop's config rather than a copy of it.
Most of that `.zshrc` is the stock oh-my-zsh comment block, and the parts that
do something are macOS-specific: Homebrew paths (`/opt/homebrew/...`, including
where spaceship lives), a `Library/Python` PATH entry, and `brew shellenv` in
`.zprofile`. Sourcing the Homebrew spaceship path on Linux errors on every
prompt, so spaceship is installed here the oh-my-zsh way instead — cloned into
`custom/themes` and symlinked where oh-my-zsh looks for it.

The laptop's `~/.zshenv` is deliberately **not** replicated: it exports a
Bitwarden Secrets Manager access token, which would give anything running here
read access to every secret backing the cluster.

### Where environment belongs

`~/.zshrc` is read only by *interactive* shells. PATH, the mise shims and the
agent credentials therefore live in `/etc/profile.d/devbox.sh`, sourced from
`/etc/zsh/zshenv` for every zsh invocation — including the non-interactive ones
agents spawn. Putting any of that in `.zshrc` would make it invisible to exactly
the shells that need it most.

### The skip-permissions alias

`devbox_claude_skip_permissions` (default `false`) controls whether `~/.aliases`
defines:

```bash
alias claude="claude --dangerously-skip-permissions"
```

It is off on purpose. The flag bypasses the permission system, which is the
thing moshi-hook forwards to the phone as approvals — with it on, those prompts
never fire. On a host that also holds a cluster-admin kubeconfig, that is worth
turning on deliberately rather than inheriting from a copied dotfile.

## Working from a phone

Two independent paths, and they stack.

**herdr over SSH.** herdr's release binaries do not support Android/Termux, so
the phone is a client that SSHes in rather than a host that runs herdr locally.
Connect over Tailscale with any SSH app and run `herdr` on the box.

**Moshi.** The [Moshi](https://getmoshi.app) app connects over Mosh (with SSH
fallback) and drives tmux or herdr sessions directly. `mosh` is installed for
this: it survives the phone roaming between cellular and Wi-Fi, which plain SSH
does not. Mosh's UDP ports need no firewall work here because the traffic rides
the tailnet.

`moshi-hook` is the companion daemon. It installs agent hook config, serves a
local Unix socket, and holds a WebSocket back to Moshi so approvals and status
reach the phone. The playbook installs it, enables lingering, installs the
Claude hooks and starts the daemon; the only manual step is the pairing token
from the app under **Settings → Hooks**, which the playbook prompts for.

Useful commands on the box:

```bash
moshi-hook status          # pairing state, hook freshness, multiplexers seen
moshi-hook probe           # is the daemon up and is the gateway connected
moshi-hook logs -f         # tail the daemon log
moshi-hook host setup      # Easy Pair, if you'd rather not configure by hand
moshi .                    # open/attach a tmux session for this directory
```

Hook config is merged into `~/.claude/settings.json` rather than overwriting it,
so unrelated settings there survive. `moshi-hook uninstall` removes just the
hook entries.

Two things about the daemon worth knowing, because both are silent when wrong:

- It runs as a **systemd user service**, so it needs
  `loginctl enable-linger michael` or it dies with your last SSH session and
  never starts at boot. The playbook enables this; `moshi-hook service install`
  on its own does not.
- Upgrades are manual (`moshi-hook update`). There is no public source repo for
  Renovate to track, so nothing bumps it for you.

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

**A shell is missing `$CLAUDE_CODE_OAUTH_TOKEN`, `herdr` or `mise`.** All three
come from `/etc/profile.d/devbox.sh`. Ubuntu's `/etc/zsh/zprofile` does *not*
source `/etc/profile`, so zsh picks it up only via the managed block in
`/etc/zsh/zshenv`. Check that block still exists; `zsh -l -c 'echo $PATH'`
should show `~/.local/share/mise/shims` first.
