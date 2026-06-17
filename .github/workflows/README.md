# GitHub Workflows

Automated PR checks for this repo. The Claude-powered reviewers use the official
[`anthropics/claude-code-action@v1`](https://github.com/anthropics/claude-code-action);
`image-pin-check.yml` is a deterministic script with no LLM and no secrets.

| Workflow | Trigger | What it does |
|---|---|---|
| `renovate-review.yml` | PRs authored by `renovate[bot]` | Reads the release notes / changelog in the PR, judges merge safety, posts a verdict comment, and **approves** clearly-safe bumps. |
| `k8s-review.yml` | Human PRs touching `kubernetes/**` | Reviews Flux / Helm / Kustomize correctness, storage classes, security context, and repo conventions (per `CLAUDE.md`). Inline + summary comments. |
| `image-pin-check.yml` | PRs touching `kubernetes/**` | Resolves every newly added `image:` reference against its registry and **fails the check** if any pinned tag/digest does not exist. Guards against typos like `teamarr:v2.6.0` (published tag is `2.6.0`) that would otherwise only surface at runtime as `ImagePullBackOff`. Runs `.github/scripts/verify-image-pins.py`. |
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

## The `new service` label

`new-service.yml` triggers on an issue gaining the label **`new service`** (exact name,
with a space). Create the label once if it doesn't exist:

```sh
gh label create "new service" --description "Scaffold this service into the cluster via Claude" --color 0E8A16
```

To use it: open an issue describing the service (name, image, port, storage, whether it needs a
web UI / secrets), then add the `new service` label. Claude opens a PR you review and merge.

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
