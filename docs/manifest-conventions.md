# Workload Manifest Conventions

The baseline every workload in this cluster is expected to meet, and the reasoning
behind each rule. This exists because the same handful of findings kept surfacing in
PR review, one new service at a time — runtime uid/gid, container hardening, resource
requests, floating tags, and monitoring registration. Encoding them here makes them a
decision already made rather than a debate re-run per PR.

Three things read this document:

- **`.claude/skills/add-service/SKILL.md`** — scaffolds new services to match it.
- **`.github/workflows/manifest-hygiene-check.yml`** — mechanically enforces the
  checkable subset with [kube-linter](https://docs.kubelinter.io), configured in
  `.kube-linter.yaml`.
- **`.github/workflows/k8s-review.yml`** — the LLM reviewer cites it instead of
  re-deriving the advice.

If you change a rule here, change it in all three places. Each section below names the
kube-linter check that enforces it, which is the stable link between them.

### What is and isn't enforced

The check lints the **rendered** tree (`flate build all`), so an app-template
HelmRelease, a hand-written Deployment and a third-party chart are all checked
identically — by the time flate is done they are all just Deployments.

It reports only findings a PR **introduces**, measured against a render of the merge
base. The tree predates this document and carries a backlog; a whole-tree gate would
fail every unrelated PR. So the backlog is exempt by construction and shrinks as
manifests get touched, but nothing here is retroactively enforced.

Run the same check locally before pushing — it needs no cluster access:

```sh
brew install mise && mise trust && mise install
mise run hygiene        # only what your branch introduces, same as CI
mise run hygiene:all    # the whole backlog, for deliberate cleanup
```

`mise.toml` pins kube-linter and flate to the versions CI uses, so a local pass means
a CI pass. Note that pointing kube-linter at the source manifests directly is *not*
equivalent: it cannot see inside a HelmRelease, and given the wrong directory it
reports zero findings and exits 0. Use the task.

Three rules in this document are **documented but not currently machine-checked**:

| Rule | Why not |
|---|---|
| `allowPrivilegeEscalation: false` (§2) | kube-linter's `privilege-escalation-container` only flags an explicit `true`; an unset field passes. |
| PUID/PGID vs `runAsUser` by image family (§1) | Conditioning on image family is homelab-specific; no linter models it. |
| Gatus / Homepage registration (§6) | Being replaced by generated config rather than a lint rule. |

These are tracked in #562. Until then they are review concerns, which is what the
`k8s-review` reviewer is for.

---

## 1. Runtime identity — how a container gets its uid/gid

**Enforced by: `run-as-non-root`**

There are two mechanisms for setting the uid/gid a container runs as, they are mutually
exclusive, and the right one is a property of the *image*, not a preference. Picking the
wrong one fails silently: the container comes up, serves traffic, and writes files as the
wrong owner.

### LinuxServer.io images (`lscr.io/linuxserver/*`, `linuxserver/*`)

Use `PUID` / `PGID` env vars. Do **not** set `runAsUser`, `runAsNonRoot`, or
`capabilities.drop: [ALL]`.

```yaml
env:
  PUID: "1000"
  PGID: "1000"
  UMASK: "022"      # optional
```

These images run `s6-overlay`, which **must start as root**: it runs init scripts, fixes
volume ownership, then re-execs the application as `PUID`/`PGID`. Forcing `runAsUser: 1000`
or dropping `ALL` capabilities breaks the init before the app ever starts.

`allowPrivilegeEscalation: false` is the one container-level control that is safe here —
s6 de-escalates, it never escalates.

Because these images legitimately start as root, `run-as-non-root` will flag them. That
is the check being right about the policy and wrong about the intent, so each such
workload records the exception on the object itself:

```yaml
metadata:
  annotations:
    ignore-check.kube-linter.io/run-as-non-root: "s6-overlay must start as root"
```

### Every other image

Use a pod-level `securityContext`. `PUID`/`PGID` on a non-LinuxServer image are **silently
ignored** — nothing reads them, and the container runs as whatever uid the image baked in
(often root). This is the single most-repeated review finding in this repo.

```yaml
# app-template
defaultPodOptions:
  securityContext:
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000

# plain Deployment/CronJob — spec.template.spec.securityContext
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
```

Reference: `kubernetes/apps/media/unpackerr/helmrelease.yaml`,
`kubernetes/apps/media/listenarr/helmrelease.yaml`.

### Images with a baked-in non-root uid

Some images already run as a fixed non-root uid (`nginx-unprivileged` → 101, the TwiN
Gatus chart → 65534). Pin *that* uid rather than forcing 1000 — overriding it breaks the
image's own file ownership. Pinning it anyway is still worth doing: it turns the non-root
posture into policy rather than a property of the current image tag.

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 101       # nginx-unprivileged default, now enforced
  runAsGroup: 101
```

### `fsGroup` vs `supplementalGroups`

Both grant group access to volume files. They are not interchangeable:

| Situation | Use | Why |
|---|---|---|
| RWO `local-path` volume the pod owns | `fsGroup: 1000` | kubelet recursively `chgrp`s the mount — cheap on a local disk, and correct when this pod is the writer. |
| NFS-backed volume (`nfs-data`, `nfs-homelab`) | `supplementalGroups: [1000]` | The recursive chgrp is slow over NFS and pointless — the NAS owns the ownership bits. |
| Volume mounted `readOnly: true` | `supplementalGroups: [1000]` | kubelet **cannot** chown a read-only mount; `fsGroup` fails outright. |
| Large NFS volume that does need fsGroup | add `fsGroupChangePolicy: OnRootMismatch` | Skips the recursive walk when the root dir already matches. |

When one pod writes files another pod reads (a CronJob producing content an nginx
Deployment serves), the reader needs group membership in the writer's gid — otherwise it
depends on world-read bits surviving whatever umask upstream happens to use.

Reference: `kubernetes/apps/civic/ames-council-digest/web-deployment.yaml`.

---

## 2. Container hardening

**Enforced by: `must-drop-all-capabilities`, `no-read-only-root-fs`**

Every container gets a container-level `securityContext`. Pod-level settings pin *who*
the process is; container-level settings bound *what it can do*. They are separate
controls and both are expected.

```yaml
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
  readOnlyRootFilesystem: true    # when the app tolerates it
```

- `allowPrivilegeEscalation: false` — always, including LinuxServer images. *Not
  machine-checked*: kube-linter only flags an explicit `true`, so an omitted field
  passes. Review still asks.
- `capabilities.drop: [ALL]` — always **except** LinuxServer images (breaks s6 init).
  *Partially machine-checked*: `must-drop-all-capabilities` evaluates containers that
  already have a `securityContext.capabilities` block, so it catches a wrong drop list
  but not a missing `securityContext` altogether.
- `readOnlyRootFilesystem: true` — whenever the app tolerates it. Most apps that write
  scratch files only need an `emptyDir` at `/tmp`:

  ```yaml
  persistence:
    tmp:
      type: emptyDir
      globalMounts:
        - path: /tmp
  ```

  Skip it for apps that write into their own install tree (many `*arr`-adjacent apps do).
  Skipping is fine; skipping *silently* is not — record the reason in the
  `ignore-check.kube-linter.io/no-read-only-root-fs` annotation.

Genuine exceptions exist (`gluetun` needs `NET_ADMIN`). Add the capability explicitly and
comment the reason; don't drop the whole block.

Reference: `kubernetes/apps/media/unpackerr/helmrelease.yaml`.

---

## 3. Resource requests and limits

**Enforced by: `unset-cpu-requirements`, `unset-memory-request`,
`unset-memory-requirements`**

Every container declares requests, and a memory limit.

```yaml
resources:
  requests:
    cpu: 10m           # scheduler hint; be honest, not generous
    memory: 128Mi
  limits:
    memory: 512Mi      # required
    # cpu: "2"         # only when you actually want a ceiling
```

- **`requests.cpu` and `requests.memory` are required.** Without them the scheduler treats
  the pod as free and this is a two-node cluster — one unbounded workload starves the rest.
- **`limits.memory` is required.** Memory is incompressible: a leaking container with no
  limit takes the node down instead of just itself.
- **`limits.cpu` is optional and usually wrong.** CPU is compressible, so a limit buys
  nothing but throttling. Set it only to deliberately cap a workload
  (`kubernetes/apps/ai/coding-agent/helmrelease.yaml` caps at 2 cores on purpose).

Homelab-scale numbers are fine — `10m` / `32Mi` for a static file server is a real answer.
The point is a declared number, not a large one.

---

## 4. Image pinning

**Enforced by: `floating-tag`**

Pin to an immutable tag or a digest. `latest`, `edge`, `stable`, `main`, `master`,
`develop`, `nightly`, `rolling`, and a bare repository with no tag are all rejected.

kube-linter's stock `latest-tag` blocks only `:latest` and untagged references, so
`.kube-linter.yaml` overrides its block list to cover the rest of the family. Keep the
two lists in sync.

A floating tag means the running version depends on when a pod last restarted, Renovate
can't propose a reviewable bump, and `deploy-canary` can't attribute a regression to a
merge. `image-pin-check.yml` separately verifies the pinned reference actually resolves in
its registry — the two checks are complementary: one says *pinned*, the other says *real*.

Digest-pinned floating tags (`:latest@sha256:…`) are accepted — the digest is what's
deployed and Renovate bumps it.

---

## 5. Health probes

**Rule ID: `probes` (advisory)**

Any workload serving HTTP gets a readiness probe. Add a liveness probe only when there's a
genuine health endpoint that means something — a liveness probe pointed at a page that
returns 200 while the app is wedged just restarts a healthy container on a timer.

Set `initialDelaySeconds` on anything that isn't instantly ready. Kubernetes starts probing
the moment the container reports Running; a Node/Python app that takes two seconds to bind
its port will fail its first probes and log a restart for no reason.

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: http
  initialDelaySeconds: 5
  periodSeconds: 10
```

Advisory, not enforced — "which endpoint means healthy" isn't mechanically decidable.

---

## 6. Monitoring registration for anything with a hostname

**Not machine-checked** — see #562. Cross-file referential integrity
between an IngressRoute and two unrelated files is not something a workload linter can
express, and the better fix is to stop requiring the edits at all: `gatus-sidecar`
generates Gatus endpoints from IngressRoute annotations, and Homepage has native
Kubernetes discovery. Until that lands this section is a review concern and a checklist.

A new `IngressRoute` means three more edits, none of which live next to the IngressRoute:

1. **Gatus endpoint** — `kubernetes/infrastructure/controllers/gatus.yaml`,
   `spec.values.config.endpoints`.
2. **Gatus `hostAliases`** — same file, in the `postRenderers` kustomize patch. In-cluster
   probes resolve `*.mpdavis.com` via the Traefik VIP, not public DNS.
3. **Homepage tile** — `kubernetes/apps/homepage/homepage/config/services.yaml` (a
   `configMapGenerator` file, *not* an inline ConfigMap).

Miss #1 or #2 and the service is invisible to `deploy-canary` and to `GatusEndpointDown`
alerting — it can be down for days without a signal. Miss #3 and it's undiscoverable.

Pick the Gatus group by whether the IngressRoute carries the `authentik-forward-auth`
middleware:

```yaml
# no forward-auth: healthy == 200
- name: <service>
  group: external-open
  url: https://<service>.mpdavis.com/
  conditions: *open-conditions

# behind authentik-forward-auth: healthy == 302 to iam.mpdavis.com
- name: <service>
  group: external-auth
  url: https://<service>.mpdavis.com/
  client: *auth-client
  headers: *auth-headers          # Accept: text/html — drives the browser redirect path
  conditions: *auth-conditions
```

A 200 on a service that should be protected means the forward-auth middleware is missing,
so the group is a real assertion, not bookkeeping.

Note the hostname formats differ by file and are not interchangeable: IngressRoutes and
the Homepage config use `${DOMAIN}` (Flux `postBuild` substitutes it); `gatus.yaml` uses
the literal `mpdavis.com` in both the endpoint URL and the `hostAliases` list.

Non-goal: a handful of hostnames are deliberately absent from Homepage (`hello-world`,
Homepage itself, machine-facing endpoints like `thumbs`). Gatus coverage has no such
exemption — everything with a hostname gets probed.

---

## 7. Chart-managed workloads

When a service uses an **official chart** rather than `app-template`, check the chart's own
defaults before overriding anything in this document. Charts frequently ship a hardened
`podSecurityContext` already, and blindly layering `fsGroup: 1000` on top can break the
app's own volume ownership — the TwiN Gatus chart ships `fsGroup: 65534` /
`runAsUser: 65534`, and forcing 1000 would have broken its SQLite PVC.

```sh
helm show values <repo>/<chart> --version <version> | grep -A10 -i securitycontext
```

The hygiene check *does* apply the structural rules to third-party charts, because it
lints the rendered output rather than the HelmRelease — it never has to know the chart's
values schema, only what the chart produced. That means a chart whose defaults violate
this document will report findings against values you did not write. Fix it by setting
the chart's own values, or, when the chart's default is the correct one for that app,
record why in an `ignore-check.kube-linter.io/<check>` annotation.

This is the one place where "check the chart's defaults first" is load-bearing: layering
`fsGroup: 1000` onto a chart that ships `65534` silences a linter and breaks the app.

---

## Exemptions

Every rule has real exceptions. To take one, annotate the **object** with the check name
and the reason:

```yaml
metadata:
  annotations:
    ignore-check.kube-linter.io/no-read-only-root-fs: "qBittorrent writes into its install tree"
```

The annotation value is the reason, and it is not optional in spirit — an exemption with a
reason is a decision on the record, which is the whole point. Silently omitting the field
is what this document exists to stop.

An annotation rather than a comment because the check runs against the **rendered** tree:
a YAML comment in a HelmRelease does not survive Helm templating, and an annotation does.
In app-template, set it under the controller's `annotations:`; the whole object is
exempted from that one check.

To exempt every check on an object, use `ignore-check.kube-linter.io/all`. Prefer naming
the specific check — "this workload is special" ages badly compared to "this workload
writes into its install tree".
