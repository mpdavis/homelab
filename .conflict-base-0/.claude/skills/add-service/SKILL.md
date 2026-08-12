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
   at Traefik via a separate `IngressRoute` (not the chart's built-in ingress). Every service
   that gets an IngressRoute also gets a Homepage tile (Step 3) — note which Homepage section it
   belongs to (Media, IPTV, Infrastructure, AI, …) and a one-line description.
7. **Storage** — what persistent storage does it need?
   - **Config/database** — `local-path`, `ReadWriteOnce`. For SQLite DBs and app config. Typical 2–5Gi.
   - **Media** — `nfs-data` storage class, or an inline NFS mount (`server: ${NAS_IP}`,
     `path: ${NAS_DATA_PATH}/media`), `ReadWriteMany`.
   - **Appdata bulk** — `nfs-homelab` storage class, `ReadWriteMany`.
   - **None** — stateless services.
8. **Secrets** — does it need secrets from Bitwarden Secrets Manager (BWS)? If so, get the BWS
   secret UUIDs and desired key names. BWS secret UUIDs are **not** hardcoded in ExternalSecrets —
   they live in the central `bws-secret-ids` ConfigMap and are referenced via `${BWS_*}`
   placeholders (Step 3).
9. **Special requirements** — GPU (`nvidia.com/gpu` + `runtimeClassName: nvidia`), node selection
   (`nodeSelector`), sidecar containers, extra environment variables, ConfigMaps, cronjob
   schedule, etc.
   - **Service-link env collision** — if the app reads its own config from env vars prefixed
     with its (upper-cased) name — e.g. `MOUSEHOLE_PORT`, `<NAME>_HOST` — they will collide with
     the legacy Docker-style **service-link** env vars Kubernetes injects for every Service in the
     namespace (`<SERVICENAME>_PORT=tcp://<clusterIP>:<port>`, `<SERVICENAME>_SERVICE_HOST`, …).
     Since this repo names the Service after the app, the injected `<NAME>_PORT` shadows the app's
     own `<NAME>_PORT` and crash-loops it (it gets a `tcp://…` URL where it expects a number). Set
     `enableServiceLinks: false` to disable the injection (the links are unused here — containers
     use explicit env + cluster DNS). See the gotcha in Step 3.

## Step 2: Place the Service (Namespace-Grouped Layout)

Services are grouped by namespace on disk: **`kubernetes/apps/<namespace>/<service>/`**. The
namespace directory owns the `Namespace` object and a group `kustomization.yaml` that sets
`namespace: <namespace>` and lists its member services. Each service is a subdirectory whose own
kustomization just lists that service's files — it inherits the namespace from the group.

```
kubernetes/apps/
  <namespace>/
    namespace.yaml          # the Namespace object
    kustomization.yaml      # namespace: <namespace>; lists namespace.yaml + ./<service> subdirs
    <service>/
      kustomization.yaml    # lists this service's files (no namespace: — inherited from group)
      helmrelease.yaml
      ...
```

Reference examples: `kubernetes/apps/media/` (the arr stack, emby, etc.) and `kubernetes/apps/ai/`
(ollama + open-webui).

Determine the placement case:

- **Case A — namespace group already exists** (`media`, `ai`, …): create
  `apps/<namespace>/<service>/` and register `./<service>` in the existing
  `apps/<namespace>/kustomization.yaml`. The namespace is already created — do **not** add another
  `namespace.yaml`.

- **Case B — brand-new namespace**: create the group directory `apps/<namespace>/` with a
  `namespace.yaml` and a group `kustomization.yaml`, plus the service subdirectory. Register the
  namespace directory in `apps/kustomization.yaml` (Step 4).

The `Namespace` object must be defined exactly once per namespace. If unsure whether it already
exists, `grep -rl "kind: Namespace" kubernetes/apps` and check.

**`apps/<namespace>/namespace.yaml`** (Case B only):
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: <namespace>
```

**`apps/<namespace>/kustomization.yaml`** (the group — sets the namespace, lists members):
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: <namespace>
resources:
  - namespace.yaml        # Case B (new namespace) only
  - ./<service>
  # ...other services in this namespace
```

> The group sets `namespace:` once and the leaf service kustomizations omit it (see `media`,
> `homepage`). The older `ai` group instead sets `namespace: ai` on each leaf — if you add a
> service under `ai`, match its siblings.

## Step 3: Create the Service Manifests

All of a service's files live in `kubernetes/apps/<namespace>/<service>/`.

### HelmRelease — generic `app-template` (default)

This is the default for any service without a dedicated chart. The `bjw-s` HelmRepository is
**already registered** (`kubernetes/infrastructure/sources/bjw-s.yaml`,
`oci://ghcr.io/bjw-s-labs/helm`) — no new source needed. Mirror
`apps/media/recyclarr/helmrelease.yaml`.

**kustomization.yaml** (the leaf — no `namespace:`; inherited from the group, Step 2):
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
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

> **Gotcha — service-link env collision.** If the app reads config from env vars prefixed with
> its own name (`<NAME>_PORT`, `<NAME>_HOST`, …), disable Kubernetes service-link env injection so
> the Service's auto-generated `<NAME>_PORT=tcp://…` vars don't shadow the app's config and
> crash-loop it. In app-template set it at the values root:
> ```yaml
> defaultPodOptions:
>   enableServiceLinks: false
> ```
> For a plain Deployment (fallback), set `spec.template.spec.enableServiceLinks: false`. This bit
> `mousehole` in the `qbittorrent` pod (its `MOUSEHOLE_PORT` got a `tcp://` URL).

### HelmRelease — official chart

When the service has its own chart, add a HelmRepository source and reference it. Mirror
`apps/ai/ollama/` (+ `infrastructure/sources/ollama.yaml`) or `apps/media/seerr/`.

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

**Homepage tile — required whenever the service has an IngressRoute.** Every service with a web
UI must also appear on the Homepage dashboard. Add an entry to the `services.yaml` block in
`kubernetes/apps/homepage/configmap.yaml`, under the section that matches its namespace/domain
(`Media`, `IPTV`, `AI`, `Infrastructure`, …):
```yaml
  services.yaml: |
    - Media:
        # ...existing services...
        - <Service Display Name>:
            icon: <service>.png          # Dashboard Icons slug; or an mdi-* icon
            href: https://<service-name>.mpdavis.com
            description: <one-line description>
```
Conventions:
- Match the `href` host to the IngressRoute's `Host(...)` rule exactly.
- Prefer a [Dashboard Icons](https://github.com/walkxcode/dashboard-icons) slug (`sonarr.png`,
  `emby.png`); fall back to a Material Design Icon (`mdi-television-classic`) when there's no logo.
- If the service belongs to a section that doesn't exist yet, add the section to **both** the
  `services.yaml` block and the `layout:` map in `settings.yaml` (same ConfigMap) so it renders.
- Homepage lives in its own namespace and reads this ConfigMap at startup — no per-service
  annotations or label-based discovery are used here; the tile is added manually.

**Gatus check — required whenever the service has an IngressRoute.** Every service with a
hostname must be probed by the synthetic-monitoring stack, in **two places** in
`kubernetes/infrastructure/controllers/gatus.yaml`:

1. An entry in `spec.values.config.endpoints`, reusing the existing anchors:
```yaml
        - name: <service-name>
          group: external-open          # service is NOT behind Authelia
          url: https://<service-name>.mpdavis.com/
          conditions: *open-conditions
```
```yaml
        - name: <service-name>
          group: external-auth          # service IS behind the authelia middleware
          url: https://<service-name>.mpdavis.com/
          client: *auth-client
          headers: *auth-headers        # Accept: text/html — makes Authelia 302 (not 401)
          conditions: *auth-conditions
```
2. The hostname added to the `hostAliases` list in the `postRenderers` patch (same file) — in-cluster
   probes resolve `*.mpdavis.com` via the Traefik VIP, not public DNS.

Pick the group by whether the IngressRoute has the `authelia` middleware: protected services are
healthy when they 302 to the auth portal; open services when they return 200. Skipping this means
the new service is invisible to the deploy canary and to `GatusEndpointDown` alerting — but note
the canary treats an endpoint with no baseline entry as must-pass, so once added, the service's
first failing deploy WILL trigger a revert PR (that's the point).

**external-secret.yaml** (only if the service needs secrets from Bitwarden).

First, **register each BWS secret UUID centrally** — never hardcode a UUID in an ExternalSecret.
Add a `BWS_*` key for every secret to the `bws-secret-ids` ConfigMap in
`kubernetes/clusters/homelab/flux-system/bws-secret-ids.yaml`, grouped under a comment for the
service:
```yaml
data:
  # <Service Name>
  BWS_<SERVICE>_<KEY>: "<bitwarden-uuid>"
```
These UUIDs are not sensitive (the actual values are fetched at runtime by ESO); the ConfigMap is
the single source of truth so an ID is defined once and reused everywhere. Flux postBuild
substitution resolves the `${BWS_*}` placeholders.

Then reference them in the ExternalSecret via `${BWS_*}` placeholders:
```yaml
apiVersion: external-secrets.io/v1
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
        key: ${BWS_<SERVICE>_<KEY>}
```
The `apps` Kustomization already lists `bws-secret-ids` in its `postBuild.substituteFrom`, so a
service under `kubernetes/apps/` needs no extra wiring. (Only if you place an ExternalSecret in a
**new** Flux Kustomization path outside `apps`/`infrastructure` would you add `bws-secret-ids` to
that Kustomization's `substituteFrom` in `kubernetes/clusters/homelab/`.)

Reference secret values inline in the app-template `env` map via `secretKeyRef:` (see
`recyclarr`), or in an official chart's values as that chart expects.

### Plain Kustomize manifests (fallback only)

Only use this when neither an official chart nor app-template fits. The pattern is a standard
`deployment.yaml` + `service.yaml` (+ `pvc.yaml`/`ingressroute.yaml`) in the service's leaf
directory (namespace inherited from the group, Step 2). Conventions: `strategy.type: Recreate`
for stateful apps, label `app: <service-name>` consistently on the pod template and Service
selector, `${TZ}`/`${NAS_IP}`/`${NAS_DATA_PATH}` for substituted values, port name `http`. See
`apps/media/sonarr` and `apps/media/emby` for examples — but prefer a HelmRelease.

## Step 4: Register the Service

1. **Namespace group kustomization** — add `./<service>` to
   `kubernetes/apps/<namespace>/kustomization.yaml` (Case A and B).
2. **Apps kustomization** — only if you created a new namespace group directory (Case B), add
   `<namespace>` to `kubernetes/apps/kustomization.yaml`:
   ```yaml
   resources:
     - ai
     - media
     - <namespace>   # ← add the new namespace dir
     - homepage
     - hello-world
   ```
3. **Sources kustomization** — for an official-chart service with a new HelmRepository, add it to
   `kubernetes/infrastructure/sources/kustomization.yaml`. (app-template needs nothing — `bjw-s`
   is already registered.)

## Step 5: Update Documentation

Update documentation **anywhere it would otherwise go stale** because of this service. Check each
of these and update the ones the change touches:

- **`docs/design.md`** — update the "Repository Structure" tree and relevant prose if the service
  introduces:
  - A new namespace — add it to the structure tree and the description
  - A new storage pattern, GPU usage, node selection, sidecars — document the decision (and add a
    row to the Decisions Log if it's a notable tradeoff)
- **`README.md`** (repo root) — if it enumerates services/namespaces, keep it in sync.
- **`CLAUDE.md`** — only if the service establishes a *new convention* future work should follow
  (a new storage class, a new namespace grouping, etc.).
- **Homepage** — already covered in Step 3 (required for any service with an IngressRoute).

If you're unsure whether a doc references the area you changed, grep for the old value (namespace
name, storage class, service name) across `docs/`, `README.md`, and `CLAUDE.md` and reconcile.

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
- If the service uses secrets, every `remoteRef.key` is a `${BWS_*}` placeholder backed by a key
  in `bws-secret-ids` (no hardcoded UUIDs; placeholder name matches a ConfigMap key exactly)
- If the service has an IngressRoute, a matching Homepage tile exists in
  `apps/homepage/configmap.yaml` and its `href` host matches the IngressRoute
- If the service has an IngressRoute, `infrastructure/controllers/gatus.yaml` has BOTH a matching
  `config.endpoints` entry (group matches the service's Authelia status) AND the hostname in the
  `hostAliases` postRenderers patch

## Checklist

Before considering the service complete, verify:

- [ ] Service directory created at `kubernetes/apps/<namespace>/<service>/`
- [ ] Deployed as a **HelmRelease** (official chart, or `app-template`) unless there's a strong reason not to
- [ ] `app-template` HelmRelease mirrors `apps/media/recyclarr` (chart `app-template`, source `bjw-s`, version pinned `~5.0`)
- [ ] Chart version pinned with a semver constraint (never `"*"`)
- [ ] Image tag/digest pinned (no `latest`/`edge` — `image-pin-check.yml` enforces this)
- [ ] Leaf `kustomization.yaml` lists exactly the files present and omits `namespace:` (inherited from the group)
- [ ] `Namespace` defined exactly once — only the group dir's `namespace.yaml` defines it (Case B)
- [ ] `./<service>` registered in `apps/<namespace>/kustomization.yaml`; new namespace group also registered in `apps/kustomization.yaml`
- [ ] Environment uses `${TZ}` (not a hardcoded timezone); NFS uses `${NAS_IP}`/`${NAS_DATA_PATH}`
- [ ] If the app reads `<NAME>_*` env vars for config, `enableServiceLinks: false` is set (avoids the service-link env collision)
- [ ] IngressRoute uses `websecure` entryPoint and `tls: {}`; Service name matches the rendered chart name
- [ ] **Service with an IngressRoute has a Homepage tile** in `apps/homepage/configmap.yaml` (correct section, `href` matches the route)
- [ ] **Service with an IngressRoute has a Gatus check** — endpoint entry (correct group: `external-open` vs `external-auth`) **and** hostname in the `hostAliases` patch, both in `infrastructure/controllers/gatus.yaml`
- [ ] PVC uses the right StorageClass (`local-path` RWO config/DB; `nfs-data`/`nfs-homelab` RWX bulk)
- [ ] For an official-chart service: HelmRepository added to `infrastructure/sources/` and its `kustomization.yaml`
- [ ] No plaintext secrets — ExternalSecret + inline `secretKeyRef` for anything sensitive
- [ ] BWS secret UUIDs registered in the central `bws-secret-ids` ConfigMap; ExternalSecret `remoteRef.key` uses a `${BWS_*}` placeholder (never a hardcoded UUID)
- [ ] Documentation reconciled where the change touches it (`docs/design.md`, `README.md`, `CLAUDE.md` — Step 5)
