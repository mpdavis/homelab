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

## Notes

- The focused reviewers run automatically and **never merge or modify files** — they
  comment (and, for Renovate, optionally approve). You stay the merge gate.
- Renovate majors and CRD/External-Secrets changes are gated behind manual dashboard
  approval in `renovate.json`, so most Renovate PRs reaching this reviewer are
  minor/patch — an unexpected major is treated as a red flag in the review.
- Models are pinned in each workflow's `claude_args` (`--model`); bump to a stronger
  model there if you want deeper judgment at higher cost.
