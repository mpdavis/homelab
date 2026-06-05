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

## Step 1: Gather Requirements

Before writing any manifests, determine the following by asking the user (skip questions where the answer is already clear from context):

1. **Service name** — lowercase, kebab-case (e.g., `bazarr`, `tautulli`, `homepage`)
2. **Deployment type** — does this service have a Helm chart we should use, or should we deploy with plain Kubernetes manifests (Deployment/Service)?
   - Check if the service has an official or well-maintained Helm chart. If yes, recommend Helm. If the service is simple (single container, no complex config), plain manifests are fine and often simpler.
3. **Container image** — full image reference (e.g., `linuxserver/bazarr:latest`)
4. **Port** — the container's primary HTTP port
5. **Namespace** — media apps go in the `media` namespace (no namespace.yaml needed — it already exists). Other services get their own namespace (need a namespace.yaml).
6. **Ingress** — does it need a web UI accessible at `<name>.mpdavis.com`?
7. **Storage** — what persistent storage does it need?
   - **Config/database** (local-path, ReadWriteOnce) — for SQLite DBs, app config. Typical size: 2-5Gi.
   - **Media** (NFS) — mount the NAS media share. Uses variable substitution: `server: ${NAS_IP}`, `path: ${NAS_DATA_PATH}/media`.
   - **Both** — most media apps need both.
   - **None** — stateless services.
8. **Secrets** — does it need secrets from Bitwarden Secrets Manager? If so, get the Bitwarden secret UUIDs and desired key names.
9. **Special requirements** — GPU (`nvidia.com/gpu`), node selection (`nodeSelector`), sidecar containers, extra environment variables, ConfigMaps, etc.

## Step 2: Create Manifests

All manifests go in `kubernetes/apps/<service-name>/`.

### Kustomize-Based App (Plain Manifests)

This is the most common pattern. Create these files:

**kustomization.yaml** — lists all resources, sets the default namespace:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: <namespace>
resources:
  # Include only the files this service actually needs.
  # Order: namespace (if needed) → external-secret → pvc → deployment → service → ingressroute
  - pvc.yaml
  - deployment.yaml
  - service.yaml
  - ingressroute.yaml
```

**deployment.yaml** — follows the established pattern exactly:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: <service-name>
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: <service-name>
  template:
    metadata:
      labels:
        app: <service-name>
    spec:
      containers:
        - name: <service-name>
          image: <image>
          ports:
            - containerPort: <port>
              name: http
          env:
            - name: PUID
              value: "1000"
            - name: PGID
              value: "1000"
            - name: TZ
              value: ${TZ}
          volumeMounts:
            - name: config
              mountPath: /config
            - name: media
              mountPath: /media
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: <service-name>-config
        - name: media
          nfs:
            server: ${NAS_IP}
            path: ${NAS_DATA_PATH}/media
```

Key conventions:
- `strategy.type: Recreate` for anything with persistent volumes (avoids multi-attach issues)
- Label: `app: <service-name>` — used consistently on the pod template and as the Service selector
- PUID/PGID `"1000"` for LinuxServer.io containers (omit for non-LinuxServer images)
- `${TZ}`, `${NAS_IP}`, `${NAS_DATA_PATH}` are substituted by Flux postBuild from the cluster-vars ConfigMap
- Port name `http` is referenced by the Service's `targetPort`
- Only include volume entries the service actually needs — not every app needs media or config

**service.yaml**:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: <service-name>
spec:
  selector:
    app: <service-name>
  ports:
    - port: <port>
      targetPort: http
      name: http
```

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

The wildcard cert for `*.mpdavis.com` is already provisioned — `tls: {}` uses it automatically.

**pvc.yaml** (only if the service needs local persistent storage):
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: <service-name>-config
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 2Gi
```

**namespace.yaml** (only if NOT using the `media` namespace):
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: <namespace>
```

**external-secret.yaml** (only if the service needs secrets from Bitwarden):
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: <secret-name>
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: <k8s-secret-name>
  data:
    - secretKey: <key-name>
      remoteRef:
        key: <bitwarden-uuid>
```

Then reference in the Deployment:
```yaml
env:
  - name: <ENV_VAR>
    valueFrom:
      secretKeyRef:
        name: <k8s-secret-name>
        key: <key-name>
```

### Helm-Based App (HelmRelease)

Use this when the service has a well-maintained Helm chart. Requires two touchpoints: a HelmRepository source and the app directory.

**1. Add a HelmRepository** in `kubernetes/infrastructure/sources/<service-name>.yaml`:
```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: <service-name>
  namespace: flux-system
spec:
  type: oci
  interval: 1h
  url: <oci-or-https-url>
```

Then add it to `kubernetes/infrastructure/sources/kustomization.yaml`.

Prefer OCI registries (`oci://`) where available. Set `interval: 1h` — chart repos don't change frequently.

**2. Create the app directory** with `kubernetes/apps/<service-name>/`:

**kustomization.yaml**:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: <namespace>
resources:
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
      chart: <chart-name>
      version: "<semver-constraint>"
      sourceRef:
        kind: HelmRepository
        name: <service-name>
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
    <inline-helm-values>
```

Pin chart versions with semver constraints — never use `"*"`. Use tilde ranges like `"~1.5"` for minor pinning.
Keep all values inline in `spec.values`. Use `spec.valuesFrom` referencing a Secret only for sensitive values.

## Step 3: Register the Service

Add the new app directory to `kubernetes/apps/kustomization.yaml` in alphabetical order:
```yaml
resources:
  - dispatcharr
  - ecm
  - emby
  - <new-service>   # ← insert alphabetically
  - hello-world
  ...
```

For Helm-based apps, also add the HelmRepository to `kubernetes/infrastructure/sources/kustomization.yaml`.

## Step 4: Update Documentation

Update `docs/design.md` if the new service introduces:
- A new namespace — add it to the description
- A new storage pattern — document it
- New infrastructure (GPU usage, node selection, sidecars) — document the decision

Update the repository structure section in `docs/design.md` to list the new app under the `apps/` tree.

## Step 5: Validate

Run `kubectl kustomize kubernetes/` or `kustomize build kubernetes/` to check that the manifests render without errors. If kustomize is not available locally, at minimum verify:
- All YAML files parse correctly
- The kustomization.yaml resource list matches the actual files
- Variable references use `${VAR}` syntax (not `$VAR` or `{{ }}`)
- Labels and selectors are consistent between Deployment, Service, and IngressRoute
- Port numbers are consistent across all manifests

## Checklist

Before considering the service complete, verify:

- [ ] App directory created at `kubernetes/apps/<name>/`
- [ ] `kustomization.yaml` lists exactly the files present (no extras, no missing)
- [ ] Deployment uses `strategy: Recreate` for stateful apps
- [ ] Labels use `app: <name>` consistently
- [ ] Environment uses `${TZ}` (not hardcoded timezone)
- [ ] NFS volumes use `${NAS_IP}` and `${NAS_DATA_PATH}` (not hardcoded IPs/paths)
- [ ] IngressRoute uses `websecure` entryPoint and `tls: {}`
- [ ] PVC uses appropriate StorageClass (`local-path` for config/DB, `nfs-data`/`nfs-homelab` for bulk)
- [ ] Service registered in `kubernetes/apps/kustomization.yaml` (alphabetical order)
- [ ] For Helm apps: HelmRepository added to `infrastructure/sources/` and its kustomization.yaml
- [ ] For Helm apps: Chart version pinned with semver constraint
- [ ] No plaintext secrets — use ExternalSecret for anything sensitive
- [ ] `docs/design.md` updated if new patterns introduced
