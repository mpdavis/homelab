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
- Persistent changes go through GitOps: the agent opens a PR, a human merges,
  doco-cd deploys
- What the agent can break is bounded by the credentials it holds, not by
  instructions in its prompt
- Self-hosted runtime; inference on the Anthropic API

## Why ntfy goes

ntfy messages are ephemeral and unthreaded, so there is nowhere to hang an
investigation or a conversation. ntfy also has a reachability gap: Grafana
Cloud reaches `ntfy.mpdavis.com` through `caddy-public` on the apps host, so an
outage of the apps VM silences the alerts about it.

## Architecture

```text
Grafana Cloud ──(native contact point)──► Discord #alerts-fallback
      ▲                                   (no homelab dependency)
      │ polls Alertmanager API (outbound only)
┌─────┴──────────── infra VM (pve1) ───────────────────────────────┐
│ hob-dispatcher ──► Discord forum: one post per alert group       │
│      │  ▲            (bot gateway: outbound websocket)           │
│      ▼  │ replies, approval buttons                              │
│ hob-agent (Claude Agent SDK)                                     │
│   reads: Grafana (read-only token), Gatus API, a clone of this   │
│          repo                                                    │
│   acts:  ssh hob@<host> → forced-command allowlist               │
│          Proxmox API token (VM.PowerMgmt on the two VMs)         │
│          GitHub App → branches and PRs                           │
└──────────────────────────────────────────────────────────────────┘
```

It runs on the infra host for the same reason Gatus and ntfy do: it has to
work while the compose host is down. The code lives in its own repo,
`mpdavis/hob`, publishing `ghcr.io/mpdavis/hob`; the stack lives in
`docker/infra/hob/`.

### Chat: Discord forum channel

Each alert group (`alertname` + `host`, matching the Grafana grouping) becomes
a forum post. Re-fires and resolves reply in the same post; tags carry state:
`firing`, `investigating`, `needs-you`, `resolved`.

Discord sits outside the homelab, so paging survives any homelab outage, and
the bot connects over an outbound websocket, so the chat side needs no inbound
port. Zulip has the better threading model (a topic per alert) and is
self-hostable, but it would be one more service that has to be up for you to
be told that services are down.

### Intake: poll, don't receive

The dispatcher polls Grafana Cloud's Alertmanager API every 30s. A webhook
would need a public endpoint, and every public endpoint today is
`caddy-public` on the apps host — the gap that sinks ntfy. Polling needs no
inbound path and costs at most 30s of latency.

The dispatcher keys threads by alert-group fingerprint and keeps the mapping
(thread ↔ group ↔ agent session) in SQLite on a named volume.

### Fallback path

The dispatcher posts a heartbeat metric. A Grafana rule on a missing heartbeat
routes through a native Discord contact point straight to `#alerts-fallback`,
bypassing the homelab entirely. Critical alerts also go to that contact point
until the dispatcher has proven itself; that duplication is removed once
phase 1 has run for a while.

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
- **Spend**: a turn and cost cap per investigation, a daily cap in the
  dispatcher, and a dedicated Anthropic workspace whose key has its own spend
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

Its own Bitwarden machine account, holding only its own secrets: the Anthropic
key, the GitHub App private key, the Discord bot token, a read-only Grafana
token and the Proxmox API token. It cannot read any other stack's secrets.

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

1. **Discord replaces ntfy** — dispatcher, forum threads, the fallback contact
   point and heartbeat rule. No agent. Retire ntfy at the end
2. **Read-only agent** — T0 only; posts an investigation into each new thread
   and answers replies
3. **Pull requests** — the GitHub App; T3
4. **Host actions** — `hob-action`, the Proxmox token, approval buttons; T1
   and T2
5. **Proactive work** — a daily sweep (Gatus, disk trends, the doco-cd image
   leak, pending reboots, failing Renovate PRs, cert expiry) and a weekly
   summary post

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Chat | Discord forum | Outside the homelab; threads; good mobile push |
| Intake | Poll Alertmanager API | No inbound path; no dependency on the apps host |
| Runtime | Claude Agent SDK on infra | Reuses repo docs and agents; survives apps outage |
| GitHub identity | App without `workflows` | Scoped, short-lived, cannot reach CI secrets |
| Host writes | SSH forced command | Allowlisted, auditable, Ansible-managed |
| T1 at launch | Gated | Loosen once its judgement has a track record |
