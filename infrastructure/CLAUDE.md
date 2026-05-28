# FluxCD Guidelines

These practices apply to all Flux-managed manifests across `infrastructure/`, `apps/`, and `clusters/`.

## API Versions

Use stable APIs only — no beta versions:

- `kustomize.toolkit.fluxcd.io/v1` for Kustomization CRs
- `helm.toolkit.fluxcd.io/v2` for HelmRelease CRs
- `source.toolkit.fluxcd.io/v1` for HelmRepository and GitRepository CRs

## HelmRelease

- One HelmRelease per directory, alongside its companion manifests
- Pin chart versions with semver constraints — never use `"*"` or omit the version
  - Critical infrastructure (cert-manager, Traefik, MetalLB): pin to minor (`"~1.17"`)
  - Applications: minor range is acceptable (`"2.x"`)
- Keep values inline in `spec.values` for non-sensitive configuration
- Use `spec.valuesFrom` referencing a Secret for sensitive values (passwords, tokens)
- Always configure install and upgrade remediation with retries:
  ```yaml
  install:
    remediation:
      retries: 3
  upgrade:
    remediation:
      retries: 3
      strategy: rollback
  ```
- Set `crds: CreateReplace` in upgrade spec so CRDs get updated on upgrade
- Use `dependsOn` between HelmReleases sparingly — only for real ordering dependencies

## Kustomization CRs (Flux Kustomizations)

- Set `prune: true` on application Kustomizations for garbage collection
- Never set `prune: true` on CRD-only Kustomizations (deleting a CRD removes all its instances)
- Use `dependsOn` for ordering between Kustomizations: `infrastructure-sources` → `infrastructure-controllers` → `infrastructure` → `apps`
- Set `interval` to `30m` or longer — frequent reconciliation is unnecessary for a homelab

## HelmRepository Sources

- Centralize all HelmRepository definitions in `infrastructure/sources/`
- Set `interval: 1h` or longer — chart repos change infrequently
- Prefer OCI registries (`oci://`) where available

## Directory Structure

- Each component gets its own directory with a `kustomization.yaml` listing its resources
- File naming: `kustomization.yaml`, `helmrelease.yaml`, descriptive names for companions (`ipaddresspool.yaml`, `clusterissuer.yaml`)
- Keep `clusters/homelab/` thin — only Flux Kustomization entrypoints and flux-system bootstrap

## Secrets

- Never commit plaintext secrets — use ExternalSecret CRs (ESO + Bitwarden Secrets Manager)
- ExternalSecret CRs contain only secret store references and UUIDs, no secret data

## FluxOperator

- Pin the Flux distribution version in FluxInstance CR — upgrade deliberately
- Do not manually edit resources managed by FluxOperator (controller Deployments, etc.)
