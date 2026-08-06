# GitHub Workflows

Automated PR checks for this repo. The Claude-powered reviewers use the official
[`anthropics/claude-code-action@v1`](https://github.com/anthropics/claude-code-action);
`image-pin-check.yml` and `deploy-health-gate.yml` are deterministic scripts with no
LLM and no secrets.

| Workflow | Trigger | What it does |
|---|---|---|
| `renovate-review.yml` | PRs authored by `renovate[bot]` | Reads the release notes / changelog in the PR, judges merge safety, posts a verdict comment, and **approves** clearly-safe bumps. |
| `k8s-review.yml` | Human PRs touching `kubernetes/**` | Reviews Flux / Helm / Kustomize correctness, storage classes, security context, and repo conventions (per `CLAUDE.md`). Inline + summary comments. |
| `image-pin-check.yml` | PRs touching `kubernetes/**` | Resolves every newly added `image:` reference against its registry and **fails the check** if any pinned tag/digest does not exist. Guards against typos like `teamarr:v2.6.0` (published tag is `2.6.0`) that would otherwise only surface at runtime as `ImagePullBackOff`. Runs `.github/scripts/verify-image-pins.py`. |
| `deploy-health-gate.yml` | All PRs | Blocks merge until the current `main` has been reconciled and reported healthy by Flux **and** verified by the deploy canary. Polls `main`'s combined GitHub commit status (which Flux's notification-controller posts per Kustomization, plus `canary/gatus` from `deploy-canary.yml`), so it needs no cluster access or self-hosted runner. Combined with the branch ruleset's "require branches up to date", this serializes merges by deploy health. |
| `deploy-canary.yml` | Push to `main` (+ manual re-run) | Waits for Flux to reconcile the merged commit, then polls the Gatus API (`status.mpdavis.com`) until every synthetic check reports a healthy result **newer than the reconcile** — proving services actually serve traffic, not just that manifests applied. Snapshots which endpoints were already failing *before* the deploy and only blames the merge for **pass→fail transitions**: those fail the `canary/gatus` commit status and **auto-open a revert PR**; pre-existing failures are exempt (chronic breakage alerts via Prometheus instead of spamming revert PRs or freezing the merge queue). A flake? Close the revert PR and re-run via workflow_dispatch. Requires the repo setting "Allow GitHub Actions to create and approve pull requests". |
| `auto-update-branch.yml` | Push to `main`, or auto-merge enabled on a PR (+ manual re-run) | Merges the new `main` into every open PR that has **auto-merge enabled** and has fallen behind. The ruleset requires branches to be up to date, so every merge stales every other PR and silently disarms its queued auto-merge; this re-arms them. Conflicting PRs are reported, never auto-resolved. Needs `BRANCH_UPDATE_TOKEN` — no-ops with a warning without it. |
| `gatus-health-report` (job in `deploy-health-gate.yml`) | All PRs | Informational, never blocks: lists which Gatus endpoints are currently failing so the PR author knows what's already red — and exempt from their merge's canary — before merging. |
| `claude.yml` | `@claude` mention in an issue/PR comment | On-demand assistant — explain, review, or make changes when asked. |
| `new-service.yml` | Issue labeled `new service` | Runs the `add-service` skill against the issue, scaffolds the manifests into `kubernetes/apps/<namespace>/<service>/`, and **opens a PR** (`Closes #<issue>`). Defaults to a HelmRelease (official chart, else bjw-s `app-template`). Never merges — the PR still goes through `k8s-review` / `image-pin-check`. |

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

### `BRANCH_UPDATE_TOKEN` (optional, only enables automatic branch updates)

`auto-update-branch.yml` merges `main` into stale auto-merge PRs. The default
`GITHUB_TOKEN` cannot do that job on two counts: it lacks the **Contents: write**
that `PUT /pulls/{n}/update-branch` requires, and — the one that actually matters —
**pushes made with it do not trigger workflows**. The branch would move forward while
`deploy-health-gate` never re-ran against the new head, leaving the PR wedged on
"Expected — Waiting for status" with nothing to clear it but another push.

So create a **fine-grained PAT** scoped to this repo with **Contents: Read and write**
and **Pull requests: Read and write**, and store it here.

- If set: stale auto-merge PRs are updated within seconds of a merge, their checks
  re-run against the new `main`, and auto-merge fires on its own.
- If absent: the workflow logs a warning and does nothing. Auto-merge PRs stay behind
  until you click "Update branch" by hand (or, for Renovate's own PRs, until Renovate's
  next run rebases them — see below).

> Every branch update is a `synchronize` event, so the PR's reviewers (`k8s-review`,
> `renovate-review`, `image-pin-check`) re-run on each one. That is the cost of keeping
> auto-merge armed; it is bounded by only ever touching PRs that opted in via auto-merge.

> Renovate covers its **own** auto-merge PRs from the other side: its default
> `rebaseWhen: "auto"` resolves to `behind-base-branch` whenever `automerge` is true,
> so it rebases stale branches on its next run. That is a scheduled fixup (up to ~an
> hour) and it skips branches it considers externally modified — this workflow reacts
> immediately and covers human PRs too. No `renovate.json` change is needed.

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
