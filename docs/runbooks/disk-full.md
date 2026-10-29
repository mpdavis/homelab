# Disk filling up

**Alert:** `HostFilesystemEarlyWarning` — a filesystem over 5G has been under
20% free for 30 minutes (`grafana-cloud/rules/docker-hosts.yml`).

The infra host's root disk is only 6.8G, so it is the one that trips this; the
compose host has 128G.

## Find what is using it

```sh
ssh root@<host>
df -h -x tmpfs -x overlay
docker system df
du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -20
journalctl --disk-usage
```

## Usual causes

| Cause | Sign | Fix |
| --- | --- | --- |
| Stale images | `docker system df` shows GBs reclaimable; `docker images` lists many tags of one image, usually `doco-cd` | `systemctl start docker-prune.service` |
| apt cache | `/var/cache/apt` in the hundreds of MB | `apt-get clean` |
| Old kernels | several `linux-image-*` in `dpkg -l 'linux-image*'` | reboot onto the newest, then `apt-get autoremove --purge` |
| Journal | `journalctl --disk-usage` over a few hundred MB | `journalctl --vacuum-size=100M` |

Container logs are capped by the daemon config (10M × 3 per container), so they
should not be the cause; if `/var/lib/docker/containers` is large anyway, check
`/etc/docker/daemon.json` against the `docker` Ansible role.

## Why `doco-cd` images pile up

doco-cd prunes unused images after each deploy, but when it updates itself the
old instance is still running while it prunes, so its own image is in use and
survives. Each Renovate bump leaves ~210MB behind. The `docker-prune.timer`
from the `docker` role removes unused images older than a week every day:

```sh
systemctl list-timers docker-prune.timer
journalctl -u docker-prune.service -n 20
```

If the timer is missing, the host was configured before it existed: pull
the primary checkout up to `main`, then re-run `ansible-playbook
playbooks/docker-host.yml --limit <host>`. Running the service once by hand
(`systemctl start docker-prune.service`) frees the space straight away.

## If cleanup is not enough

Grow the disk: raise `disk_size` for the VM in `tofu/proxmox/variables.tf`
and `tofu -chdir=tofu/proxmox apply` from the primary checkout. Ubuntu's
cloud image grows the root partition and filesystem on the next boot.
