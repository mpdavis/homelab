# GitHub Workflows

Automated PR checks for this repo. The Claude-powered reviewers use the official
[`anthropics/claude-code-action@v1`](https://github.com/anthropics/claude-code-action);
`render-validate.yml`, `image-pin-check.yml` and `deploy-canary.yml` are deterministic
scripts with no LLM and no secrets.

| Workflow | Trigger | What it does |
|---|---|---|
| `render-validate.yml` | All PRs + push to `main` | Renders the whole tree offline the way Flux would — every Kustomization and HelmRelease, with real chart sources and `postBuild` substitution — then schema-validates the result and comments the **rendered** diff. Catches what only surfaced at reconcile time before: a patch whose target does not exist, a resource missing from a `kustomization.yaml`, a chart version that resolves to nothing, an unresolved `${VAR}`, or a field typo the API server would reject. See [Render & Validate](#render--validate) below. |
| `renovate-review.yml` | PRs authored by `renovate[bot]` | Reads the release notes / changelog in the PR, judges merge safety, posts a verdict comment, and **approves** clearly-safe bumps. |
| `k8s-review.yml` | Human PRs touching `kubernetes/**` | Reviews Flux / Helm / Kustomize correctness, storage classes, security context, and repo conventions (per `CLAUDE.md`). Inline + summary comments. |
| `image-pin-check.yml` | All PRs (**required check**) | Resolves every newly added `image:` reference against its registry and **fails the check** if any pinned tag/digest does not exist. Guards against typos like `teamarr:v2.6.0` (published tag is `2.6.0`) that would otherwise only surface at runtime as `ImagePullBackOff`. Runs `.github/scripts/verify-image-pins.py`. Its `verify` job is the required status check on main's ruleset, so it runs on **every** PR (no `paths` filter) — a required check that never runs blocks the PR forever. On PRs touching no manifests it exits 0 in ~5s. |
| `deploy-canary.yml` | Push to `main` (+ manual re-run) | Waits for Flux to reconcile the merged commit, then polls the Gatus API (`status.mpdavis.com`) until every synthetic check reports a healthy result **newer than the reconcile** — proving services actually serve traffic, not just that manifests applied. Snapshots which endpoints were already failing *before* the deploy and only blames the merge for **pass→fail transitions**: those fail the `canary/gatus` commit status and **auto-open a revert PR**; pre-existing failures are exempt (chronic breakage alerts via Prometheus instead of spamming a revert PR per merge). Post-merge only — a red canary reverts, it does not block the next PR. A flake? Close the revert PR and re-run via workflow_dispatch. Requires the repo setting "Allow GitHub Actions to create and approve pull requests". |
| `claude.yml` | `@claude` mention in an issue/PR comment | On-demand assistant — explain, review, or make changes when asked. |
| `new-service.yml` | Issue labeled `new service` | Runs the `add-service` skill against the issue, scaffolds the manifests into `kubernetes/apps/<namespace>/<service>/`, and **opens a PR** (`Closes #<issue>`). Defaults to a HelmRelease (official chart, else bjw-s `app-template`). Never merges — the PR still goes through `k8s-review` / `image-pin-check`. |
| `lint-dockerfile.yml` | PRs touching `docker/**/Dockerfile` (advisory) | hadolint over the changed Dockerfiles. Config `.hadolint.yaml`. See [Lint checks](#lint-checks). |
| `lint-shell.yml` | PRs touching `**/*.sh` (advisory) | shellcheck over the changed shell scripts. Config `.shellcheckrc`. |
| `lint-markdown.yml` | PRs touching `**/*.md` (advisory) | markdownlint-cli2 over the changed Markdown files. Config `.markdownlint-cli2.jsonc`. |
| `lint-secrets.yml` | All PRs + push to `main` (advisory, no `paths` filter) | gitleaks over the commit range the PR/push adds. Config `.gitleaks.toml`. Backstop for a credential that bypasses the External Secrets pattern. |
| `lint-helm.yml` | PRs touching `charts/**` (advisory) | `helm lint --strict` over first-party charts under `charts/*/`. Dormant until the first local chart lands. |

## Lint checks

Five stack-wide linters, added together. All are **advisory** — none is on main's
ruleset — and all are **`paths`-filtered** to the file type they own, so this is the
opposite of the required checks: a lint workflow that does not run is fine, and the
filter keeps it off PRs it has nothing to say about. On a pull request each one lints
only the files that PR changed (`git diff` against the base SHA), so the existing
backlog in docs and images does not wall off unrelated work; `workflow_dispatch` runs
the same tool over the whole tree for a deliberate cleanup pass.

| Tool | Scope | Config | Notes |
|---|---|---|---|
| [hadolint](https://github.com/hadolint/hadolint) | `docker/**/Dockerfile` | `.hadolint.yaml` | `DL3008/DL3013/DL3018` (distro package pinning) and `DL4006` start disabled — the images pin *tool* versions via renovate-annotated ARGs and leave the distro package set to the base image. |
| [shellcheck](https://github.com/koalaman/shellcheck) | tracked `*.sh` | `.shellcheckrc` | Scripts embedded in workflow `run:` blocks are **not** covered — that needs actionlint, which this repo does not run yet. |
| [markdownlint-cli2](https://github.com/DavidAnson/markdownlint-cli2) | `*.md` | `.markdownlint-cli2.jsonc` | `MD013`/`MD033`/`MD041` disabled; vendored trees (`.claude/`, `.venv`, caches) in `ignores`. |
| [gitleaks](https://github.com/gitleaks/gitleaks) | whole repo, commit range only | `.gitleaks.toml` | No `paths` filter — a leak can be anywhere. Allowlists lockfiles, test fixtures, BWS UUIDs, and the git-ignored local files. |
| [helm](https://helm.sh) `lint --strict` | `charts/*/` | — | No first-party charts exist yet; third-party charts are HelmReleases and `render-validate.yml` already covers those. Starts linting automatically when `charts/<name>/` appears. |

**Version pins** live as renovate-annotated `*_VERSION` env vars in each workflow —
the same custom manager that tracks `kubeconform` in `render-validate.yml` keeps
these current (see `renovate.json`).

**Promoting one to required.** Get the tool to green over the whole tree
(`workflow_dispatch`), then — exactly as with `Render & Validate` — drop the `paths`
filter (a required check that gets skipped never reports and blocks the PR forever)
and add the job to main's ruleset.

## Render & Validate

The deterministic pre-merge counterpart to the canary. Everything else here checks
manifests either semantically (the Claude reviewers, which read intent) or after the
fact (the canary, once `main` has already moved). Nothing rendered them, so a broken
patch target or an ignored Helm value merged green and only failed at reconcile —
and because the `apps` Kustomization deliberately has no `wait:`, that could show up
as a Gatus alert rather than a Flux error.

Three stages, run by [`flate`](https://github.com/home-operations/flate) (a Go
rewrite of the archived `flux-local`) and
[`kubeconform`](https://github.com/yannh/kubeconform):

1. **`flate test all`** — reconciles all 5 Kustomizations and every HelmRelease
   offline, resolving real chart sources and applying `postBuild` substitution from
   the in-repo `cluster-vars` / `bws-secret-ids` ConfigMaps. **Blocking.**
2. **`kubeconform`** — validates the rendered objects against the Kubernetes schemas
   plus the [CRD catalog](https://github.com/datreeio/CRDs-catalog), which covers the
   whole CRD surface here (IngressRoute, ExternalSecret, PrometheusRule,
   ServiceMonitor, Certificate, ClusterIssuer, TLSStore, IPAddressPool, …).
   **Blocking.** Catches a PVC with `storageClass:` where the field is
   `storageClassName:` — valid YAML, renders fine, rejected by the API server.
3. **`flate diff`** — renders the PR and its base and posts the **rendered** delta as
   a single sticky comment, so a chart bump shows the lines of the Deployment it
   actually changes rather than a version string. **Advisory, never blocks.**

No cluster access and no secrets — flate links helm, kustomize and the Flux source
SDKs as libraries and pulls charts from their public repositories.

**Making it required.** The check is `Render & Validate / render`. Add it to main's
ruleset alongside `verify`. Note that Renovate's `automerge` only gates on **required**
checks — a check that merely runs does not hold auto-merge back, so until it is marked
required it will not stop a bad patch bump from landing.

**No `paths` filter, deliberately.** Same lesson as `image-pin-check` (#425) and
`ames-council-digest-tests`: a required check that gets skipped never reports, and
GitHub waits for it forever. On a PR touching no manifests flate renders from cache in
a couple of seconds.

**The sticky comment** is keyed on a `<!-- render-validate -->` marker and edited in
place on every push (`.github/scripts/render-validate-comment.sh`). A PR that renders
clean and changes no rendered output gets **no comment at all** — the quiet path is the
common one. Bodies are truncated to stay under GitHub's 65 536-character limit.

**Running it locally:**

```sh
brew install --cask home-operations/tap/flate
flate test all --path ./kubernetes            # does it reconcile?
flate diff all --path ./kubernetes --base main # what changes in the cluster?
flate build all --path ./kubernetes | kubeconform -strict -summary \
  -ignore-missing-schemas -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

> `--base` needs a normal clone: as of flate 0.5.0 it cannot resolve `HEAD` inside a
> **git worktree**, where `.git` is a file rather than a directory, and fails with
> `resolve HEAD: reference not found`. CI is unaffected (`actions/checkout` produces a
> real `.git`), but `/new-task`-style worktrees need `flate test` / `flate build`
> without `--base`, or a plain clone for the diff.

**The `base: ""` on the install step is load-bearing.** The flate action's `base` input
defaults to the repository's default branch and exports it as `FLATE_BASE` in
`GITHUB_ENV`, which every later `flate` invocation picks up *implicitly* — putting
`test` and `build` into changed-only mode. A PR touching no manifests then renders
nothing and still exits 0. The first run of this workflow did exactly that: it reported
`✓ 0 passed · 23 skipped` and went green. Only the diff step wants a baseline, and it
passes `--base` explicitly.

**Vacuity guards.** Because of the above, neither tool's exit code is trusted on its
own: the job asserts that flate reconciled a non-zero number of resources and that
kubeconform found a non-zero number to validate, and fails if either covered an empty
set. A green check that validated nothing is worse than no check, because it gets
trusted.

**Speed.** A fully cold render of this repo — all 5 Kustomizations, 29 HelmReleases,
18 HelmRepositories, 536 rendered objects — measures **~5 seconds** and produces a
~7 MB cache, so the job is dominated by runner startup rather than by rendering.

**Known flakiness in flate 0.5.0 — read this before debugging a slow run.** flate has
been observed **wedging** rather than failing. Twice locally, and once in CI where
`flate build` sat for 11+ minutes on a tree that `flate test` had rendered seconds
earlier in the same job. It is not repo-specific and not reproducible on demand.

Mitigations, none of them a proven fix:

- `FLATE_CONCURRENCY: 8` — the default of 40 parallel reconcile bodies is a lot to
  schedule onto a 4-vCPU runner, and narrows the window for whatever the stall is.
- **Per-step `timeout-minutes`** (5 for render and build, 8 for diff) on top of the
  15-minute job cap, so a wedge fails in minutes and names the step that hung instead
  of reporting a generic job timeout.
- The diff step is additionally `continue-on-error` — it is advisory, so it must never
  hold the check hostage.

A known-pathological case is at least understood: pinning a chart to a version matching
no published tag sends flate enumerating the registry's entire tag list, which hangs
rather than failing fast. If this proves noisy in practice, the fallback is to drop to
a single `flate build` invocation (halving the render work) or to pin an older release.

**Version pins.** The flate action is pinned by tag (Renovate's `github-actions`
manager keeps it current); `KUBECONFORM_VERSION` is a `# renovate:`-annotated env var
picked up by the workflow custom manager in `renovate.json`.

## Required secrets

Set these in **Settings → Secrets and variables → Actions**.

### `CLAUDE_CODE_OAUTH_TOKEN` (required, all workflows)

Subscription token for Claude. Generate it locally and paste the value:

```sh
claude setup-token
```

### `CLAUDE_REVIEW_TOKEN` (optional, only enables Renovate approval)

GitHub blocks the default `GITHUB_TOKEN` from **approving** pull requests, so the
"approve safe ones" step in `renovate-review.yml` needs a separate identity. Create
a **fine-grained PAT** (or a GitHub App installation token) scoped to this repo with
**Pull requests: Read and write**, and store it here.

- If set: Claude submits a real approval review on bumps it judges safe to merge.
- If absent: the workflow still posts its advisory comment; it just can't approve.

> A PAT approval comes from *your* account, so it won't satisfy a branch-protection
> rule that requires review from someone other than the author. It's a signal /
> convenience, not a way to self-approve past required reviews.

## The `new service` label

`new-service.yml` triggers on an issue gaining the label **`new service`** (exact name,
with a space). Create the label once if it doesn't exist:

```sh
gh label create "new service" --description "Scaffold this service into the cluster via Claude" --color 0E8A16
```

To use it: open an issue with the **"Add a new service"** template
(`.github/ISSUE_TEMPLATE/add-service.yml`) and fill in just three things — the service
name, the target namespace, and a documentation link. The template auto-applies the
`new service` label on submit, so no manual labeling step is needed. The workflow then
researches the service from the documentation link (image, port, storage, ingress,
secrets), scaffolds it per the `add-service` skill, and opens a PR you review and merge.
You can still apply the label by hand to any free-form issue if you prefer.

## Notes

- `new-service.yml` is the one Claude workflow that **writes code** (on the labeled issue's
  behalf) — it pushes a branch and opens a PR, but never merges. The focused reviewers below
  run automatically and **never merge or modify files** — they comment (and, for Renovate,
  optionally approve). You stay the merge gate.
- Renovate majors and CRD/External-Secrets changes are gated behind manual dashboard
  approval in `renovate.json`, so most Renovate PRs reaching this reviewer are
  minor/patch — an unexpected major is treated as a red flag in the review.
- Models are pinned in each workflow's `claude_args` (`--model`); bump to a stronger
  model there if you want deeper judgment at higher cost.
