# Hob

Hob is the SRE agent: it picks up homelab alerts, investigates them, talks them through
in a per-alert thread, and proposes fixes as pull requests. It runs as its own
persona: none of Michael's credentials, no merge rights, and host access only
through an allowlist.

This page is the design. It is built in phases (below); each phase is useful
on its own.

## Goals

- Every alert gets a first investigation before a human looks at it
- One threaded, persistent space per alert — findings, follow-ups and the
  resolve all land in the same place
- Being paged keeps working while either VM, or the whole homelab, is down
  (once IRM pages; see Paging)
- Persistent changes go through GitOps: the agent opens a PR, a human merges,
  doco-cd deploys
- What the agent can break is bounded by the credentials it holds, not by
  instructions in its prompt
- Self-hosted runtime; inference on the Anthropic API

## Paging

ntfy stays the page for now. Its messages are ephemeral and unthreaded, so it
cannot hold an investigation, and Grafana Cloud reaches it through
`caddy-public` on the apps host, so an apps-VM outage silences the alerts about
it. Both go away when IRM's escalation chain (below) takes over paging through
the Grafana mobile app; that is deliberately a later step.

## Architecture

```text
Grafana Alerting ──► ntfy                         (paging, for now)
      │
      └──► Grafana IRM ── alert group ──► outgoing webhooks ──► Discord forum
           (grouping, state,              (open post; reply     #homelab-alerts
            resolution notes)              on status change)        │
                ▲                                                   │ bot gateway
                │ resolution notes                                  │ (outbound)
┌───────────────┴─────────── infra VM (pve1) ───────────────────────▼──┐
│ hob (Claude Agent SDK + Discord bot)                                 │
│   reads: Grafana (read-only token), Gatus API, a clone of this repo  │
│   acts:  ssh hob@<host> → forced-command allowlist                   │
│          Proxmox API token (VM.PowerMgmt on the two VMs)             │
│          GitHub App → branches and PRs                               │
└──────────────────────────────────────────────────────────────────────┘
```

Hob runs on the infra host for the same reason Gatus and ntfy do: it has to
work while the compose host is down. The code lives in its own repo,
`mpdavis/hob`, publishing `ghcr.io/mpdavis/hob`; the stack lives in
`docker/infra/hob/`.

### Alert groups: Grafana IRM

Every alert also goes to a Grafana IRM integration (`tofu/grafana/irm.tf`).
IRM owns what a hand-built dispatcher would have: grouping (`alertname` +
`host`, from the notification policy), state (firing, acknowledged, resolved,
silenced) and history. Its default route has no escalation chain yet, so it
pages nobody.

A re-fire after a resolve is a new alert group, so it gets a new post.

### Threads: IRM webhooks into a Discord forum

Two IRM outgoing webhooks, no homelab code:

- **Alert group created** posts to the forum's Discord webhook with
  `thread_name` and `wait=true`. That opens a forum post and returns its first
  message, whose `channel_id` is the post
- **Status change** posts to the same webhook with
  `thread_id={{ responses["<open webhook>"].channel_id }}`: IRM keeps every
  webhook's response per alert group, so the acknowledge, resolve or re-fire
  lands inside the original post

The opening message carries an ``IRM alert group: `<id>` `` line, which is how
Hob ties a post back to IRM.

Discord sits outside the homelab, so the threads survive any homelab outage.
IRM's webhooks do not retry, so a Discord failure can lose a post or a reply;
IRM stays the record. Forum tags need a bot-authenticated call, so Hob manages
them, not the webhooks.

### Intake: the forum is the queue

Hob connects to Discord as a bot over the gateway, an outbound websocket. A
new post in the forum is a new alert group to investigate; a human reply in a
post is a follow-up. No public endpoint and no polling, so nothing depends on
`caddy-public` or the apps host.

When an investigation concludes, Hob writes it to the alert group as an IRM
resolution note, so the record outlives the Discord thread.

### Agent runtime

The Claude Agent SDK: Claude Code's tool loop, subagents, hooks and permission
callbacks as a library. The agent works from a clone of this repo, so it
starts with the `CLAUDE.md` files, `.claude/agents/diagnose-service.md` and
`docs/runbooks/` — most of what it needs to know is already written down.

- **Sessions**: each Discord thread maps to an SDK session. A reply in the
  thread resumes that session with its full context
- **Models**: Sonnet for triage; Opus when an investigation escalates or when
  asked for in the thread
- **Debounce**: warnings wait 5 minutes before an investigation starts, so a
  flapping alert costs nothing; criticals start at once
- **Spend**: a turn and cost cap per investigation, a daily cap in Hob, and a dedicated Anthropic workspace whose key has its own spend
  limit

## Identity

### GitHub: an App, not a user

A GitHub App, `hob-homelab`, installed on this repo only, with `contents: write` and
`pull_requests: write`. Its tokens are short-lived and its commits show as
`hob-homelab[bot]`.

It is **not** granted `workflows: write`. Without it GitHub rejects any push
that touches `.github/workflows/`, which closes the obvious escalation: a
branch whose workflow reads the repository's secrets.

It cannot merge. The ruleset on `main` and the required `verify` check apply to
it like anyone else, and doco-cd deploys only `main`, so nothing it writes
reaches a host without a human merge.

### Secrets

Hob's credentials are its own: the Anthropic key, the Discord bot token, a
read-only Grafana token, its SSH key, and later the GitHub App private key and
the Proxmox API token. They live in Bitwarden like any stack's secrets, and
doco-cd injects them into the `hob` stack at deploy time through
`docker/infra/hob/.doco-cd.yml`. Hob holds no Bitwarden token, so it cannot
read any other stack's secrets.

## Host access

Autonomy is tiered, and each tier is enforced by what the agent can call:

| Tier | Examples | Gate |
|---|---|---|
| T0 read | `docker ps/logs/inspect`, `df`, `journalctl -u`, Grafana, Gatus | automatic |
| T1 safe ops | restart one container, `docker image prune` | approval button (to be loosened to automatic) |
| T2 disruptive | restart doco-cd, reboot a VM | approval button |
| T3 config | anything persistent | PR; a human merges |

Host commands go over SSH as a user `hob` whose `authorized_keys` entry is
`command="/usr/local/bin/hob-action",restrict`. `hob-action` accepts only
allowlisted verbs and arguments, and is installed by its own Ansible role.

- No docker-socket-proxy with write access: container create is root
  on the host, and a proxy cannot allow restart without it
- `hob-action inspect` drops `Config.Env`. doco-cd injects secrets as
  environment variables; without the filter every one of them would land in
  the agent's context and in API requests
- VM reboots go through a Proxmox API token whose role grants `VM.PowerMgmt`
  and `VM.Audit` on the two VMs and nothing else

### Untrusted input

The agent reads text attackers can write: Caddy logs, user agents, error
messages from public services. The defence is the boundary above — no secrets
in reach, no merge, a verb allowlist, a human for T1–T3 — not prompt
instructions. The agent container's egress is limited to the Anthropic API,
GitHub, Discord, Grafana Cloud and the two hosts.

## Learning

Every investigation that finds something new ends with a PR to the matching
`docs/runbooks/` page (or a new one). What the agent learns lives in git where
both of us can read and correct it, not in an opaque memory store.

## Phases

1. **Alert threads** — IRM integration and the two Discord webhooks
   (`tofu/grafana/irm.tf`). No homelab code
2. **Read-only agent** — Hob as a Discord bot; T0 only; posts an investigation
   into each new post, answers replies, writes IRM resolution notes
3. **Pull requests** — the GitHub App; T3
4. **Host actions** — `hob-action`, the Proxmox token, approval buttons; T1
   and T2
5. **Proactive work** — a daily sweep (Gatus, disk trends, the doco-cd image
   leak, pending reboots, failing Renovate PRs, cert expiry) and a weekly
   summary post
6. **Paging moves to IRM** — an escalation chain to the Grafana mobile app;
   ntfy retires

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Alert state | Grafana IRM | Grouping, state and history off the shelf; free for one user |
| Chat | Discord forum, fed by IRM webhooks | Outside the homelab; one thread per alert group; no code |
| Intake | Hob watches the forum over the gateway | No inbound path; no dependency on the apps host |
| Runtime | Claude Agent SDK on infra | Reuses repo docs and agents; survives apps outage |
| GitHub identity | App without `workflows` | Scoped, short-lived, cannot reach CI secrets |
| Host writes | SSH forced command | Allowlisted, auditable, Ansible-managed |
| T1 at launch | Gated | Loosen once its judgement has a track record |
