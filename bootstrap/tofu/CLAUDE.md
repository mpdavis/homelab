# OpenTofu

Three root modules, each with its own local state:

| Root | Manages |
|---|---|
| `proxmox/` | LXC containers and VMs on pve1/pve2 |
| `cloudflare/` | DNS records for services the compose hosts serve |
| `grafana/` | where the Grafana Cloud stack's alerts go: ntfy contact points and the notification policy |

## State lives in the primary checkout

State and `terraform.tfvars` are local files, git-ignored, and exist **only** in
`~/git/homelab` — never in a worktree. Run against that path:

```sh
tofu -chdir=~/git/homelab/bootstrap/tofu/cloudflare plan
```

Pull that checkout first, or you plan against stale config.

## Credentials

All three read them from the environment, so nothing lands in a file:

```sh
export PROXMOX_VE_PASSWORD=...                                   # proxmox/
export CLOUDFLARE_API_TOKEN="$(bws secret get 67c9d80b-ca8e-47b5-a2eb-b442005fab6a -o json | jq -r .value)"  # cloudflare/
export GRAFANA_AUTH=...                                          # grafana/: stack service account token
export TF_VAR_ntfy_token="$(bws secret get 47079c89-adab-4d19-8173-b48d01492747 -o json | jq -r .value)"  # grafana/
```

## DNS records

`cloudflare/` owns only what the compose hosts serve. ExternalDNS owns the rest
and deletes a record when its IngressRoute goes away, so cutting a service over
means: merge the removal from `kubernetes/`, wait for ExternalDNS to delete the
record, then add the hostname to `records` here and apply. Adding it earlier
just gets it reset — ExternalDNS still owns it through its TXT registry.

`targets` names the destination rather than repeating an address: `public` is
the router (which forwards 443 to the compose host's public Caddy), the others
are Caddy IPs reachable over the LAN and the Tailscale subnet route.

An existing record is adopted, not recreated:

```sh
tofu -chdir=... import 'cloudflare_dns_record.service["<host>"]' '<zone_id>/<record_id>'
```

## Provider upgrades

The lock file is git-ignored, so a Renovate bump to a `version` constraint needs
`tofu init -upgrade` before the next plan, which otherwise fails with
"Inconsistent dependency lock file".
