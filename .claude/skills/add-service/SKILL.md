---
name: add-service
description: >
  Scaffold and integrate a new service into this homelab k3s/FluxCD GitOps repository.
  Use this skill whenever the user wants to add, deploy, install, or set up a new application
  or service in the homelab cluster — whether it's a media app, utility, database, dashboard,
  monitoring tool, or anything else. Also use when the user says things like "deploy X",
  "set up X in the cluster", "add X to kubernetes", or "I want to run X". This skill ensures
  the new service follows all established patterns, conventions, and FluxCD best practices
  from the existing codebase.
user-invocable: true
argument-hint: "[service-name]"
arguments: [service_name]
---

# Add Service to Homelab

Add a new service to the k3s cluster following the exact patterns established in this repository.

**Default to a HelmRelease.** Every new service should be a HelmRelease unless there is a
strong reason not to. Services with an official/well-maintained chart use that chart;
everything else uses the generic **bjw-s `app-template`** chart (already proven in this repo by
`recyclarr`). Standardizing on HelmReleases is deliberate — it is what lets us move toward
automatically testing services on deploy. Plain Kustomize manifests are now a fallback, not the
default.

## Step 1: Gather Requirements

Before writing any manifests, determine the following by asking the user (skip questions where the answer is already clear from context):

1. **Service name** — lowercase, kebab-case (e.g., `bazarr`, `tautulli`, `homepage`)
2. **Namespace** — which namespace this belongs to. Group related services together:
   - Media stack (arr apps, players, download clients, request managers) → `media`
   - AI/inference → `ai`
   - Anything genuinely new → its own namespace named after the domain
3. **Deployment type** — default is **HelmRelease**. Decide which chart:
   - **Official chart** — if the service publishes (or has a well-maintained community) Helm
     chart, use it. Prefer OCI registries (`oci://`).
   - **app-template** (default fallback) — if there is no good dedicated chart, use the bjw-s
     `app-template` chart. This covers the common "single container + config volume + maybe an
     ingress" case and keeps everything HelmRelease-shaped. Only drop to plain Kustomize
     manifests if app-template genuinely can't express what's needed (rare).
4. **Container image** — full image reference, pinned (e.g., `ghcr.io/recyclarr/recyclarr:7.4.1`).
   Never `latest`/`edge` for new services — pin a tag or digest (the `image-pin-check.yml`
   workflow will fail the PR otherwise).
5. **Port** — the container's primary HTTP port
6. **Ingress** — does it need a web UI at `<name>.mpdavis.com`? This repo terminates all ingress
   at Traefik via a separate `IngressRoute` (not the chart's built-in ingress).
7. **Storage** — what persistent storage does it need?
   - **Config/database** — `local-path`, `ReadWriteOnce`. For SQLite DBs and app config. Typical 2–5Gi.
   - **Media** — `nfs-data` storage class, or an inline NFS mount (`server: ${NAS_IP}`,
     `path: ${NAS_DATA_PATH}/media`), `ReadWriteMany`.
   - **Appdata bulk** — `nfs-homelab` storage class, `ReadWriteMany`.
   - **None** — stateless services.
8. **Secrets** — does it need secrets from Bitwarden Secrets Manager? If so, get the Bitwarden
   secret UUIDs and desired key names.
9. **Special requirements** — GPU (`nvidia.com/gpu` + `runtimeClassName: nvidia`), node selection
   (`nodeSelector`), sidecar containers, extra environment variables, ConfigMaps, cronjob
   schedule, etc.

## Step 2: Place the Service (Namespace-Grouped Layout)

Services are grouped by namespace on disk: **`kubernetes/apps/<namespace>/<service>/`**. A
namespace directory owns the `Namespace` object and a kustomization that lists its member
services; each service is a subdirectory whose own kustomization sets `namespace: <namespace>`.

```
kubernetes/apps/
  <namespace>/
    namespace.yaml          # the Namespace object (see exception below)
    kustomization.yaml      # lists ./<service> subdirs (+ namespace.yaml)
    <service>/
      kustomization.yaml    # namespace: <namespace>; lists this service's files
      helmrelease.yaml
      ...
```

Reference example: `kubernetes/apps/ai/` (groups `ollama` + `open-webui`).

Determine the placement case:

- **Case A — namespace group already exists** (e.g., `apps/ai/`): create
  `apps/<namespace>/<service>/` and register `./<service>` in the namespace's
  `kustomization.yaml`. The namespace is already created — do not add another `namespace.yaml`.

- **Case B — brand-new namespace**: create the group directory `apps/<namespace>/` with its own
  `namespace.yaml` + `kustomization.yaml`, plus the service subdirectory. Register the namespace
  directory in `apps/kustomization.yaml` (Step 4).

- **Case C — namespace exists but only as legacy flat apps** (this is `media` today — the
  media apps `sonarr`, `radarr`, `emby`, … still live at the top level, and the `media`
  Namespace object is created by `apps/emby/namespace.yaml`): create the group directory
  `apps/media/<service>/` for the new service, but **do not** add a `namespace.yaml` to
  `apps/media/` — the namespace is already defined elsewhere and duplicating it breaks the
  kustomize build. The new group's `kustomization.yaml` just lists `./<service>`. Leave the
  existing flat media apps untouched.

> Rule of thumb: a group dir includes `namespace.yaml` **only if no other manifest already
> defines that Namespace**. When in doubt, `grep -rl "kind: Namespace" kubernetes/apps` and
> check whether the target namespace already appears.

**`apps/<namespace>/namespace.yaml`** (Case B only):
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: <namespace>
```

**`apps/<namespace>/kustomization.yaml`** (the group — lists member services):
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml        # include only in Case B
  - ./<service>
  # ...other services in this namespace
```
Note: the group kustomization does **not** set `namespace:` — each service subdirectory sets its
own.

## Step 3: Create the Service Manifests

All of a service's files live in `kubernetes/apps/<namespace>/<service>/`.

### HelmRelease — generic `app-template` (default)

This is the default for any service without a dedicated chart. The `bjw-s` HelmRepository is
**already registered** (`kubernetes/infrastructure/sources/bjw-s.yaml`,
`oci://ghcr.io/bjw-s-labs/helm`) — no new source needed. Mirror `apps/recyclarr/helmrelease.yaml`.

**kustomization.yaml**:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: <namespace>
resources:
  # Order: external-secret → configmap → pvc → helmrelease → ingressroute
  - pvc.yaml
  - helmrelease.yaml
  - ingressroute.yaml
```

**helmrelease.yaml**:
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: <service-name>
spec:
  interval: 30m
  chart:
    spec:
      chart: app-template
      version: "~5.0"          # pin to the app-template major; never "*"
      sourceRef:
        kind: HelmRepository
        name: bjw-s
        namespace: flux-system
  install:
    remediation:
      retries: 3
  upgrade:
    remediation:
      retries: 3
      strategy: rollback
    crds: CreateReplace
  values:
    controllers:
      main:
        # type: cronjob        # for scheduled jobs (see recyclarr); omit for long-running
        containers:
          main:
            image:
              repository: <image-repo>     # e.g. ghcr.io/org/app
              tag: <pinned-tag>            # pin — no latest/edge
            env:
              TZ: ${TZ}
              # PUID: "1000"               # LinuxServer.io images only
              # PGID: "1000"
              # SOME_API_KEY:              # secret value, inline secretKeyRef:
              #   secretKeyRef:
              #     name: <service-name>-secrets
              #     key: some-api-key
    service:
      main:
        controller: main
        ports:
          http:
            port: <port>
    persistence:
      config:
        existingClaim: <service-name>      # references the PVC from pvc.yaml
        globalMounts:
          - path: /config
      # media:                             # inline NFS for the media share
      #   type: nfs
      #   server: ${NAS_IP}
      #   path: ${NAS_DATA_PATH}/media
      #   globalMounts:
      #     - path: /media
```

app-template conventions (v5):
- `controllers.main.containers.main` is the canonical name pair; `service.main.controller: main`
  wires the Service to it.
- `env` is a **map** (`TZ: ${TZ}`), and secret values use an inline `secretKeyRef:` block under
  the env key (see `recyclarr`).
- `persistence.<name>.existingClaim` references a PVC you define in `pvc.yaml`. Use
  `globalMounts` for a simple mount, `advancedMounts` to target a specific container/path.
- Leave the chart's `ingress:` disabled — this repo uses a standalone Traefik `IngressRoute`.
- `${TZ}`, `${NAS_IP}`, `${NAS_DATA_PATH}` are substituted by Flux postBuild from cluster-vars.

### HelmRelease — official chart

When the service has its own chart, add a HelmRepository source and reference it. Mirror
`apps/ai/ollama/` (+ `infrastructure/sources/ollama.yaml`) or `apps/seerr/`.

**1. Add a HelmRepository** in `kubernetes/infrastructure/sources/<service-name>.yaml`:
```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: <service-name>
  namespace: flux-system
spec:
  type: oci                  # omit `type` for classic https chart repos
  interval: 1h
  url: <oci-or-https-url>
```
Then register it in `kubernetes/infrastructure/sources/kustomization.yaml`. Prefer `oci://`.

**2. helmrelease.yaml** — same `interval`/`install`/`upgrade` block as above, but point
`chart.spec.chart` + `sourceRef.name` at the official chart, and pin the version with a semver
constraint (e.g. `"~3.6"`, `"1.x"`). Keep values inline under `spec.values`; use
`spec.valuesFrom` referencing a Secret only for sensitive values.

### Supporting manifests (both HelmRelease paths)

**pvc.yaml** (config/database storage):
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: <service-name>
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 2Gi
```
Storage classes: `local-path` (RWO, config/DB), `nfs-data` (RWX, media), `nfs-homelab` (RWX,
bulk appdata).

**ingressroute.yaml** (only if the service has a web UI):
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: <service-name>
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`<service-name>.mpdavis.com`)
      kind: Rule
      services:
        - name: <service-name>
          port: <port>
  tls: {}
```
The `*.mpdavis.com` wildcard cert is already provisioned — `tls: {}` uses it automatically. The
`apps/kustomization.yaml` patch auto-adds the ExternalDNS target annotation, so a DNS record is
provisioned automatically — no per-service annotation needed. (Note: app-template names its
Service `<service-name>` via the chart's fullname; verify the rendered Service name and match it
here.)

**external-secret.yaml** (only if the service needs secrets from Bitwarden):
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: <service-name>-secrets
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: <service-name>-secrets
  data:
    - secretKey: <key-name>
      remoteRef:
        key: <bitwarden-uuid>
```
Reference secret values inline in the app-template `env` map via `secretKeyRef:` (see
`recyclarr`), or in an official chart's values as that chart expects.

### Plain Kustomize manifests (fallback only)

Only use this when neither an official chart nor app-template fits. The pattern is a standard
`deployment.yaml` + `service.yaml` (+ `pvc.yaml`/`ingressroute.yaml`), with the service
directory's `kustomization.yaml` setting `namespace: <namespace>`. Conventions: `strategy.type:
Recreate` for stateful apps, label `app: <service-name>` consistently on the pod template and
Service selector, `${TZ}`/`${NAS_IP}`/`${NAS_DATA_PATH}` for substituted values, port name
`http`. See the legacy flat media apps (`apps/sonarr`, `apps/emby`) for examples — but prefer a
HelmRelease.

## Step 4: Register the Service

1. **Namespace group kustomization** — add `./<service>` to
   `kubernetes/apps/<namespace>/kustomization.yaml` (Case A/B/C).
2. **Apps kustomization** — if you created a new namespace group directory (Case B/C first
   service), add `<namespace>` to `kubernetes/apps/kustomization.yaml` in alphabetical order:
   ```yaml
   resources:
     - ai
     - dispatcharr
     - <namespace>   # ← insert alphabetically if new
     ...
   ```
3. **Sources kustomization** — for an official-chart service with a new HelmRepository, add it to
   `kubernetes/infrastructure/sources/kustomization.yaml`. (app-template needs nothing — `bjw-s`
   is already registered.)

## Step 5: Update Documentation

Update `docs/design.md` if the new service introduces:
- A new namespace — add it to the description
- A new storage pattern — document it
- New infrastructure (GPU usage, node selection, sidecars) — document the decision

Update the repository structure section in `docs/design.md` to list the new app under the
`apps/<namespace>/` tree.

## Step 6: Validate

Run `kubectl kustomize kubernetes/` or `kustomize build kubernetes/` to check that the manifests
render without errors. If kustomize is not available locally, at minimum verify:
- All YAML files parse correctly
- Each `kustomization.yaml` resource list matches the actual files / subdirectories present
- The `Namespace` object is defined exactly once for the target namespace (no duplicates)
- Variable references use `${VAR}` syntax (not `$VAR` or `{{ }}`)
- For app-template: `controllers`/`service`/`persistence` keys are consistent and the
  `existingClaim` matches the PVC name
- Port numbers are consistent across HelmRelease/Service and IngressRoute
- The image tag is pinned (no `latest`/`edge`)

## Checklist

Before considering the service complete, verify:

- [ ] Service directory created at `kubernetes/apps/<namespace>/<service>/`
- [ ] Deployed as a **HelmRelease** (official chart, or `app-template`) unless there's a strong reason not to
- [ ] `app-template` HelmRelease mirrors `apps/recyclarr` (chart `app-template`, source `bjw-s`, version pinned `~5.0`)
- [ ] Chart version pinned with a semver constraint (never `"*"`)
- [ ] Image tag/digest pinned (no `latest`/`edge` — `image-pin-check.yml` enforces this)
- [ ] Service subdir `kustomization.yaml` sets `namespace: <namespace>` and lists exactly the files present
- [ ] `Namespace` defined exactly once — group dir includes `namespace.yaml` only if nothing else defines it (not for `media`)
- [ ] `./<service>` registered in `apps/<namespace>/kustomization.yaml`
- [ ] New namespace group registered in `apps/kustomization.yaml` (alphabetical)
- [ ] Environment uses `${TZ}` (not a hardcoded timezone); NFS uses `${NAS_IP}`/`${NAS_DATA_PATH}`
- [ ] IngressRoute uses `websecure` entryPoint and `tls: {}`; Service name matches the rendered chart name
- [ ] PVC uses the right StorageClass (`local-path` RWO config/DB; `nfs-data`/`nfs-homelab` RWX bulk)
- [ ] For an official-chart service: HelmRepository added to `infrastructure/sources/` and its `kustomization.yaml`
- [ ] No plaintext secrets — ExternalSecret + inline `secretKeyRef` for anything sensitive
- [ ] `docs/design.md` updated if new patterns/namespaces introduced
