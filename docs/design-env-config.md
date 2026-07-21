# Externalizing Environment-Specific Configuration

Design for moving deployment-specific values (IP addresses, domain, timezone,
node names, storage paths) out of the git repository into a central,
out-of-git source of truth.

## Goals

- No deployment-specific value hardcoded in tracked files
- One place to change per concern, not N files to search/replace
- Reuse: someone can fork this repo and stand up their own homelab by
  supplying their own config, without editing tracked manifests
- Keep the existing Flux `postBuild.substituteFrom` mechanism — it already works
- Stay lightweight; don't add a stateful service just to hold a handful of vars

## The core constraint: two tiers

Environment-specific config splits into two tiers, and **no single tool spans
both**, because one exists before the cluster and one after it.

| Tier | Consumed by | When | Can it read from the cluster? |
|---|---|---|---|
| **Bootstrap** | OpenTofu, Ansible | Before k3s exists, from a workstation | No |
| **Runtime** | Flux / Kubernetes | During reconciliation, in-cluster | Yes |

A runtime value can live in the cluster (ConfigMap, Secret, External Secrets).
A bootstrap value cannot — there is no cluster yet when Tofu provisions LXC
containers or Ansible installs k3s. This boundary is inherent to the
architecture and dictates that the solution is **layered**: one mechanism per
tier.

## What exists today

Runtime config is already partially externalized via a central ConfigMap and
Flux variable substitution:

- `kubernetes/clusters/homelab/flux-system/cluster-vars.yaml` — a `ConfigMap`
  holding `NAS_IP`, `NAS_DATA_PATH`, `NAS_HOMELAB_PATH`, `TZ`.
- `kubernetes/clusters/homelab/infra.yaml` and `apps.yaml` — Flux Kustomizations
  reference it via `postBuild.substituteFrom`, resolving `${NAS_IP}` etc. in
  manifests at reconcile time.
- `bws-secret-ids` ConfigMap does the same for Bitwarden secret UUIDs.

The mechanism is sound. Two gaps:

1. **`cluster-vars.yaml` is tracked in git.** It is a resource in
   `flux-system/kustomization.yaml`, so Flux applies it from the repo. The
   values are visible to anyone with the repo, and a forker inherits
   `10.0.1.6` / `America/Chicago`.
2. **Most env-specific values are not parameterized at all.** The domain
   `mpdavis.com` is hardcoded in 19+ IngressRoutes; the contact email in 3
   places; the GitHub repo URL in 2; the MetalLB pool in 1. These never flow
   through substitution.

## Inventory of what must be externalized

### Runtime tier (Flux/Kubernetes)

| Value | Where | Status today |
|---|---|---|
| `NAS_IP`, `NAS_DATA_PATH`, `NAS_HOMELAB_PATH` | 21 manifests | Parameterized (in-git ConfigMap) |
| `TZ` | 24+ manifests | Parameterized (in-git ConfigMap) |
| Domain (`mpdavis.com`) | 19+ IngressRoutes, Authentik, external-dns, cert-manager | **Hardcoded** |
| Contact email | cert-manager, Authentik | **Hardcoded (×2)** |
| GitHub repo URL | flux-instance, Ansible bootstrap | **Hardcoded (×2)** |
| MetalLB VIP pool (`10.0.1.200-250`) | metallb IPAddressPool | **Hardcoded** |
| Authentik OIDC client ID (non-secret) | blueprint + paperless | **Hardcoded** |
| Bitwarden secret UUIDs | `bws-secret-ids` ConfigMap | Parameterized (in-git, user-specific) |

### Bootstrap tier (Tofu/Ansible)

| Value | Where | Status today |
|---|---|---|
| Proxmox node IPs (`10.0.1.1/2`) | `tofu/proxmox/variables.tf` defaults, `ansible/inventory/hosts.yml` | Tofu var (default); Ansible static |
| k3s node IPs / VMIDs (`10.0.1.50-52`, `200-202`) | same | Tofu var (default); Ansible static |
| Gateway, CIDR, DNS servers | `variables.tf` defaults; hardcoded in LXC/VM Ansible roles | Mixed |
| Node names (`pve1/2`, `k3s-*`) | Tofu + Ansible | Static |
| SSH public keys, Proxmox endpoint | `tofu/proxmox/terraform.tfvars` | Externalized (gitignored) |

Note `terraform.tfvars` is already gitignored — the bootstrap tier is
half-solved, following the right pattern.

## Tool evaluation

### Runtime tier

**A. Out-of-band ConfigMap** — remove `cluster-vars` from git; seed it into the
cluster at bootstrap (Ansible or `kubectl apply`). Flux `substituteFrom`
references it by name and does not care what created it.

- Pros: near-zero change to existing architecture; substitution plumbing already
  works; values truly leave git; no new dependency.
- Cons: the ConfigMap becomes undeclared cluster state ("hidden config"); must be
  re-seeded on cluster rebuild; drift is invisible to Flux.

**B. Bitwarden Secrets Manager + External Secrets** — put the runtime vars in
BWS (already used for 18 secrets); an ExternalSecret materializes them into the
ConfigMap that `substituteFrom` reads.

- Pros: one external source of truth already operated; rebuild-safe (re-syncs);
  consistent with the secrets story.
- Cons: putting non-secrets in a secrets manager is a category smell; adds a
  sync-ordering dependency (ExternalSecret must resolve before dependent
  Kustomizations); couples to BWS availability.

**C. SOPS + age** — encrypt `cluster-vars` in git; Flux decrypts at reconcile.

- Pros: canonical GitOps answer; values stay reviewable as diffs; rebuild-safe.
- Cons: **does not meet the goal** — data stays *in* git (encrypted, but present),
  so a forker still cannot use it and cannot decrypt it. Right for secrets, wrong
  for reuse-driven externalization.

**D. Vault / OpenBao + External Secrets** — central KV store feeding the cluster.

- Pros: purpose-built; the only option that also covers the bootstrap tier (Tofu
  `vault` provider, Ansible lookup); audit/versioning.
- Cons: heavy for a homelab; a stateful service to run, unseal, and back up —
  adding infra to *remove* a ConfigMap.

### Bootstrap tier

**E. Gitignored var files** — extend the existing `terraform.tfvars` pattern:
move node IPs/gateway/DNS out of `variables.tf` defaults into a gitignored
`*.auto.tfvars`, and templatize the static `inventory/hosts.yml` to read
gitignored `group_vars`.

- Pros: native to both tools; no new dependency; the standard approach.
- Cons: values duplicated between Tofu and Ansible unless a shared file is used;
  nothing enforces the files stay populated.

**F. One shared gitignored file** (e.g. `bootstrap/env.yaml`) read by Tofu via
`yamldecode(file(...))` and Ansible via `vars_files`.

- Pros: the literal "one central spot outside git"; DRY across Tofu + Ansible.
- Cons: doesn't reach the runtime tier (K8s can't read a workstation file);
  slightly non-idiomatic Tofu.

**G. mise (jdx.dev)** — the polyglot tool-version manager doubling as a
per-directory env manager and task runner. Env-specific values go in a
gitignored `mise.local.toml` (its documented home for local/secret overrides),
either as `[env]` entries or loaded from a file via `_.file` (dotenv, YAML,
JSON, or TOML). mise auto-exports them when you `cd` into the repo, so Tofu picks
them up as `TF_VAR_*` and Ansible/`kubectl` inherit them with no wrapper. It can
also encrypt that file with SOPS/age and decrypt on load, and its task runner can
wrap the bootstrap steps (`mise run bootstrap`).

- Pros: single gitignored source (like F) but *automatically* loaded into the
  shell — no `source`/`-var-file` plumbing; committed `mise.toml` documents which
  vars exist while `mise.local.toml` holds the values; native `TF_VAR_*` bridge;
  built-in SOPS/age if any bootstrap value needs encrypting; the task-runner
  angle can also orchestrate seeding the runtime ConfigMap.
- Cons: another tool every operator must install; still doesn't reach the runtime
  tier (K8s can't consult mise); env only loads inside a mise-activated shell, so
  CI/cron paths must invoke it explicitly; overlaps with plain gitignored files
  (F) if you don't already use mise for tool versions.

### Cross-cutting

**H. Infisical / Doppler** — config+secret platforms with Tofu, Ansible, and
K8s-operator integrations.

- Pros: purpose-built; spans both tiers; UI/versioning.
- Cons: new dependency (possibly a cloud account); overlaps heavily with the
  Bitwarden Secrets Manager already in use.

## Decision

Layered, reusing what already runs — no new platform:

- **Runtime tier → A (out-of-band ConfigMap).** Remove `cluster-vars.yaml` from
  the `flux-system` kustomization and `.gitignore` it; seed it at bootstrap.
  Smallest change, keeps the working substitution mechanism, and gets the values
  out of git. (B remains a clean upgrade later if in-cluster config drift becomes
  a problem — the manifests don't change, only what populates the ConfigMap.)
- **Bootstrap tier → F (single gitignored `env.yaml`)** shared by Tofu and
  Ansible, plus templatizing the static Ansible inventory.

If mise (G) is already in use for tool versions, it is the preferred way to
*implement* F rather than a separate choice: the gitignored file becomes
`mise.local.toml`, values auto-load as `TF_VAR_*`/env on `cd`, and a
`mise run bootstrap` task can also seed the runtime ConfigMap. Absent an existing
mise investment, a plain gitignored `env.yaml` keeps the dependency count at
zero.

This yields two out-of-git sources — one file for bootstrap, one ConfigMap for
runtime — split along the pre/post-cluster boundary that already exists.
Collapsing to a single store is only possible with Vault/OpenBao (D), reserved
for if/when Vault is wanted for its own sake.

SOPS (C) is explicitly rejected here: it keeps data in git and so does not serve
the reuse goal, though it remains the right tool for actual secrets.

## Implementation outline

Tool choice is orthogonal to parameterization — the hardcoded values must be
lifted into substitution vars regardless. Order:

1. **Parameterize runtime values (largest task).** Add `DOMAIN`, `EMAIL`,
   `GIT_REPO_URL`, `METALLB_POOL` (and optionally the Authentik client ID) to
   `cluster-vars`, and replace the 19+ hardcoded `mpdavis.com` / email / repo-URL
   references with `${...}` placeholders. Ensure every Kustomization touching
   these paths lists `cluster-vars` in `substituteFrom` (apps + infra already do;
   verify `infrastructure-controllers`, which currently only substitutes
   `cluster-vars` — good — and any path that gains a `${DOMAIN}`).
2. **Take `cluster-vars` out of git.** Remove it from
   `kubernetes/clusters/homelab/flux-system/kustomization.yaml`, add
   `cluster-vars.yaml` to `.gitignore`, and commit a
   `cluster-vars.yaml.example` template. Seed the real ConfigMap via an Ansible
   task (or documented `kubectl apply`) during bootstrap.
3. **Bootstrap tier.** Create gitignored `bootstrap/env.yaml` (+ committed
   `.example`); read it from Tofu (`yamldecode`) and Ansible (`vars_files`);
   remove env-specific *defaults* from `variables.tf`; templatize
   `inventory/hosts.yml` from `group_vars`.
4. **Docs.** Update `design.md` to point new users at the two `.example` files as
   the only things they edit to reuse the repo.

### Reuse checklist (end state)

A forker edits exactly two files, both gitignored:

- `bootstrap/env.yaml` — node IPs, names, VMIDs, gateway/DNS, SSH keys, Proxmox
  endpoint, repo URL.
- `kubernetes/clusters/homelab/flux-system/cluster-vars.yaml` — NAS, TZ, domain,
  email, MetalLB pool, BWS UUIDs.

No tracked manifest contains a deployment-specific value.

## Risks / trade-offs

- **Undeclared cluster state.** The out-of-band ConfigMap is not in git, so a
  cluster rebuild that skips the seeding step leaves Flux substituting empty
  strings. Mitigation: make seeding an explicit, idempotent Ansible bootstrap
  task, not a manual `kubectl` step; document it as a rebuild prerequisite.
- **Silent substitution failures.** Flux fills unresolved `${VAR}` with empty
  unless the Kustomization sets it to fail. Consider `postBuild` strictness or a
  CI check that every `${VAR}` used has a key in the example ConfigMap.
- **Two sources, not one.** Accepted: the pre/post-cluster split makes a single
  store impossible without adding Vault.
- **BWS UUIDs remain user-specific.** Even externalized, a forker must recreate
  their own Bitwarden secrets and IDs — unavoidable and out of scope here.
