---
name: save-secret
description: >
  Store a credential in Bitwarden Secrets Manager (BWS) with the `bws` CLI and return its UUID
  for use in the homelab stack. Use this skill IMMEDIATELY and PROACTIVELY whenever the user
  supplies a live credential in chat — API key, API token, access token, OAuth token or client
  secret, password, webhook URL, database connection string, private key, session cookie,
  license key, or any value they call "secret" — even when they do not explicitly ask for it to
  be saved. Also use when the user says "save this secret", "put this in Bitwarden", "add this
  to BWS", "store this key", "I need the secret ID for X", or when a new or existing service
  needs an ExternalSecret whose BWS UUID does not exist yet, and when rotating a credential that
  is already in BWS.
user-invocable: true
argument-hint: "[SECRET_KEY_NAME]"
---

# Save a Secret to Bitwarden Secrets Manager

The cluster never holds secret material in git. External Secrets Operator (ESO) pulls values
from BWS at runtime; this repo only ever stores **UUIDs**. This skill is the one path a secret
takes from the user's message into BWS, and it ends by handing back the UUID plus the manifest
lines that reference it.

## Rules — apply these before anything else

1. **Never write a secret value into a file in this repo.** Not into a manifest, not into a
   `.env`, not into a comment, not "temporarily". The only thing that lands in git is the UUID.
2. **Never echo the value back** to the user, into a commit message, or into a PR body. Confirm
   with the key name, the UUID, and the fingerprint the script prints — never the value.
3. **Never pass the value as a command-line argument.** Process arguments are visible to `ps`
   and land in shell history. Always pipe it on stdin using a quoted heredoc (Step 3).
4. **One secret per BWS entry.** Do not pack a username and password into one value — create
   `FOO_ADMIN_USER` and `FOO_ADMIN_PASSWORD` separately, as the Grafana and Paperless entries do.
5. **Tell the user to rotate anything they pasted in chat if it is high-value.** The value is in
   the conversation transcript regardless of what this skill does with it. Say so once, plainly,
   at the end — do not lecture.

## Step 1: Prerequisites

`bws` must be on PATH and `BWS_ACCESS_TOKEN` must be exported with a machine-account token that
has **write** access to the homelab project. If either is missing, stop and ask the user to run:

```sh
brew install bitwarden/tap/bws        # if not installed
export BWS_ACCESS_TOKEN='<machine account token>'
```

Do not try to mint a token yourself, and do not read the token out of the cluster.

The homelab project and org are fixed, and are already recorded in
`kubernetes/infrastructure/external-secrets/clustersecretstore.yaml`:

| | |
|---|---|
| organizationID | `e4568968-aebc-4a1a-be2a-b3e001118e1b` |
| projectID | `07445573-1608-4b6b-8f80-b3e0012078de` |

**A secret created outside that project is invisible to ESO** — the ClusterSecretStore is scoped
to it. The helper script defaults to it; only override with `--project-id` if the user asks.

## Step 2: Choose the key name

**The BWS key name is a human label only.** ESO resolves secrets by **UUID** —
`remoteRef.key` holds the UUID, never the name. So the BWS key and the `BWS_*` ConfigMap key do
not have to match, renaming a BWS entry breaks nothing, and the name exists purely so a human
can find the thing in the Bitwarden web vault.

For **new** secrets use `UPPER_SNAKE_CASE`, prefixed with the service:

```
TAILSCALE_OAUTH_CLIENT_SECRET     PAPERLESS_OIDC_CLIENT_SECRET
AUTHENTIK_SECRET_KEY              TREK_ENCRYPTION_KEY
```

The project has **mixed historical styles** — `Sonarr - API Key`, `cf-dns-api-token`,
`emby_api_key`, `Grafana - Admin Password` all predate that convention. **Do not rename them**;
the rename buys nothing and risks confusing a human looking for a familiar label. Just use the
new style for anything you add.

Check what already exists before inventing a name, so a rotation does not become a duplicate:

```sh
bws --color no --output json secret list 07445573-1608-4b6b-8f80-b3e0012078de | jq -r '.[].key' | sort
```

> **`bws secret list` returns every secret's plaintext value.** Always pipe it through `jq -r
> '.[].key'` as above so only names reach your context, and never paste raw list output back to
> the user. `--color no` is required — see Troubleshooting.

## Step 3: Store it

Pipe the value on stdin with a **quoted** heredoc (`<<'EOF'` — the quotes stop the shell from
expanding `$`, backticks and backslashes inside the secret):

```sh
.claude/skills/save-secret/scripts/bws-save-secret.sh BAZARR_API_KEY \
  --note 'Bazarr API key — Settings > General > API Key; regenerate there if leaked' <<'EOF'
<the value the user gave you>
EOF
```

The script creates the secret, reads it back, and verifies the round trip by fingerprint. It
prints two lines:

```
id=4c1f9a20-77bd-4f0e-9a31-b4a2003c91de
sha256=9f2a1c4b7e03
```

The `--note` is not decoration — it is how future-you regenerates the credential. Record where
the value came from (which console, which page) and any scopes or expiry it carries.

**Rotating an existing secret:** the script refuses to overwrite by default and exits 3 with the
existing UUID. Re-run with `--rotate` to replace the value. The **UUID does not change on a
rotation**, so no manifest edit is needed — only an ESO refresh (Step 6). To rotate one of the
older mixed-case entries, add `--legacy-key` so the name check does not reject it:

```sh
.claude/skills/save-secret/scripts/bws-save-secret.sh 'Sonarr - API Key' \
  --rotate --legacy-key --note 'rotated <date>' <<'EOF'
<new value>
EOF
```

## Step 4: Report the UUID

Give the user the key name and UUID. Do not repeat the value.

## Step 5: Wire it into the stack

Unless the user only asked to store the value, finish the job:

**a. Register the UUID** in `kubernetes/clusters/homelab/flux-system/bws-secret-ids.yaml`, in a
commented group with related keys:

```yaml
  # Bazarr API key — Settings > General > API Key. Used by homepage widgets.
  BWS_BAZARR_API_KEY: "4c1f9a20-77bd-4f0e-9a31-b4a2003c91de"
```

The `BWS_*` name is yours to choose — it is the Flux substitution variable, not a lookup into
BWS. Keep it descriptive and grouped with related keys under a comment, as the file already does.

**b. Reference it from an ExternalSecret** as a `${BWS_*}` placeholder — never the bare UUID:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: bazarr-api-key
  namespace: media
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: bazarr-api-key
  data:
    - secretKey: api-key            # the key inside the resulting k8s Secret
      remoteRef:
        key: ${BWS_BAZARR_API_KEY}  # a UUID after substitution — ESO looks up by ID
```

**c. Confirm the owning Flux Kustomization substitutes the ConfigMap.** The `${BWS_*}`
placeholder is resolved by Flux postBuild, not by ESO — without this the manifest ships with a
literal `${BWS_...}` and the ExternalSecret silently never syncs:

```yaml
  postBuild:
    substituteFrom:
      - kind: ConfigMap
        name: cluster-vars
      - kind: ConfigMap
        name: bws-secret-ids
```

`apps.yaml` and `infra.yaml` in `kubernetes/clusters/homelab/` already have it; check any other
Kustomization you add an ExternalSecret under.

**d. Add the ExternalSecret to the service's `kustomization.yaml`** — first in the resource
order, ahead of the workload that consumes it.

## Step 6: Verify it landed (needs cluster access)

After Flux reconciles:

```sh
kubectl get externalsecret -n <ns> <name>
kubectl describe secret -n <ns> <name>      # key names + byte counts, no values
```

`SecretSynced` on the ExternalSecret means ESO reached BWS and the value is in the cluster, and
a non-zero byte count on the expected key confirms it is not empty. Do **not** `get secret -o
yaml` or decode `.data` to check — that prints the credential; the byte count answers the
question without it. On a **rotation**, force a
refresh instead of waiting out `refreshInterval`:

```sh
kubectl annotate externalsecret -n <ns> <name> force-sync="$(date +%s)" --overwrite
kubectl rollout restart deployment/<name> -n <ns>   # if the app reads the value only at boot
```

If the cluster is unreachable, ask Michael to hop on the VPN rather than hunting for workarounds.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `bws` exits `Access token is not valid` | `BWS_ACCESS_TOKEN` unset, expired, or from another org |
| Secret created but ESO reports `not found` | created outside project `07445573-…` — recreate it there |
| ExternalSecret stuck, manifest shows literal `${BWS_FOO}` | owning Kustomization is missing `bws-secret-ids` in `substituteFrom` |
| Value in the cluster has a trailing newline | the heredoc added one; the script strips trailing newlines, a manual `bws secret create` does not |
| `jq: Invalid numeric literal` piping `bws` output | `bws` emits ANSI color codes even into a pipe. Pass `--color no` on every machine-read call (the script's `bwsj` helper does this) |
| `bws secret create` rejects the arguments | the CLI changed its argument form — run `bws secret create --help` and follow it; the script probes for both the positional and flag forms |

## Do not

- Do not create Kubernetes `Secret` manifests with `stringData` in this repo.
- Do not add a secret value to `cluster-vars` — that ConfigMap is world-readable config.
- Do not commit a placeholder like `REPLACE-ME-...` and call the task done; either the real UUID
  goes in or you tell the user the value is still missing.
