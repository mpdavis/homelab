# GitHub Workflows

Automated PR checks for this repo. The Claude-powered ones use the official
[`anthropics/claude-code-action@v1`](https://github.com/anthropics/claude-code-action);
`image-pin-check.yml` and `validate-stacks.yml` are deterministic scripts with no LLM.

| Workflow | Trigger | What it does |
|---|---|---|
| `image-pin-check.yml` | All PRs (**required check**) | Resolves every newly added `image:` reference under `docker/stacks/`, `docker/infra/` and `docker/doco-cd/` against its registry and **fails the check** if a pinned tag or digest does not exist — a typo there otherwise only surfaces when doco-cd fails to pull. Runs `.github/scripts/verify-image-pins.py`. Its `verify` job is the required status check on main's ruleset, so it runs on **every** PR (no `paths` filter): a required check that never runs blocks the PR forever. |
| `validate-stacks.yml` | PRs touching the compose trees | Runs `.github/scripts/validate-stacks.sh`: renders every compose file, checks every `external_secrets` UUID, runs `caddy validate` on each Caddyfile against the pinned image, and rejects a hostname served twice or monitoring agent configs that differ between hosts. |
| `renovate-review.yml` | PRs authored by `renovate[bot]` | Reads the release notes in the PR, judges merge safety, posts a verdict comment, and **approves** clearly-safe bumps. |
| `new-service.yml` | Issue labeled `new service` | Runs the `add-service` skill against the issue and **opens a PR** scaffolding the stack, Caddy site, Gatus checks and DNS record (`Closes #<issue>`). Never merges. |
| `claude.yml` | `@claude` mention in an issue/PR comment | On-demand assistant — explain, review, or make changes when asked. |
| `publish-images.yml` | Pushes to `main` touching `images/**` | Builds and publishes the images under `images/` to `ghcr.io/mpdavis/`. See `images/CLAUDE.md`. |
| `grafana-cloud.yml` | PRs + push to `main` touching `grafana-cloud/**` (advisory) | Validates the Grafana Cloud alert rules with `mimirtool` on PRs; on `main`, syncs them into the stack's Grafana as Grafana-managed rules. Their notification routing is `tofu/grafana`, not this workflow. |
| `lint-shell.yml` | PRs touching `**/*.sh` (advisory) | shellcheck over the changed shell scripts. Config `.shellcheckrc`. |
| `lint-markdown.yml` | PRs touching `**/*.md` (advisory) | markdownlint-cli2 over the changed Markdown files. Config `.markdownlint-cli2.jsonc`. |
| `lint-secrets.yml` | All PRs + push to `main` (advisory, no `paths` filter) | gitleaks over the commit range the PR/push adds. Config `.gitleaks.toml`. Backstop for a credential that bypasses the `.doco-cd.yml` pattern. |

## Lint checks

The three linters are **advisory** — none is on main's ruleset — and the shell and
Markdown ones are **`paths`-filtered** to the file type they own. On a pull request
each lints only the files that PR changed, so an existing backlog does not wall off
unrelated work; `workflow_dispatch` runs the same tool over the whole tree for a
deliberate cleanup pass.

| Tool | Scope | Config | Notes |
|---|---|---|---|
| [shellcheck](https://github.com/koalaman/shellcheck) | tracked `*.sh` | `.shellcheckrc` | Scripts embedded in workflow `run:` blocks are **not** covered — that needs actionlint, which this repo does not run yet. |
| [markdownlint-cli2](https://github.com/DavidAnson/markdownlint-cli2) | `*.md` | `.markdownlint-cli2.jsonc` | `MD013`/`MD033`/`MD041` disabled; vendored trees (`.claude/`, `.venv`, caches) in `ignores`. |
| [gitleaks](https://github.com/gitleaks/gitleaks) | whole repo, commit range only | `.gitleaks.toml` | No `paths` filter — a leak can be anywhere. Allowlists lockfiles, test fixtures, Bitwarden UUIDs, and the git-ignored local files. |

**Version pins** live as renovate-annotated `*_VERSION` env vars in each workflow,
kept current by a custom manager in `renovate.json`.

**Promoting one to required.** Get the tool to green over the whole tree
(`workflow_dispatch`), then drop the `paths` filter (a required check that gets
skipped never reports and blocks the PR forever) and add the job to main's ruleset.
