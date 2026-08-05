# ames-council-digest

Watches the City of Ames' public document repository, summarizes each new City
Council meeting's agenda and packet with an LLM, and emits a short digest.

Deployed as a CronJob from `kubernetes/apps/civic/ames-council-digest/`.

## How it works

The city publishes to Laserfiche WebLink 11, whose Angular browse UI is backed
by two unauthenticated endpoints this tool uses directly — no scraping, no
headless browser:

| Endpoint | Purpose |
|---|---|
| `POST /WebLink/FolderListingService.aspx/GetFolderListing2` | folder contents |
| `GET /WebLink/0/edoc/<entryId>/x.pdf` | raw PDF bytes |

Documents live under `Clerk Files` (folder `236500`) in parallel trees, all
keyed by meeting date:

```
Clerk Files/Agendas/City Council/<Year>/<YYYY MMDD>/   the agenda PDF
Clerk Files/Council Packet/<Year>/<YYYY MMDD>/         one PDF per agenda item
Clerk Files/Minutes/City Council/<Year>/<YYYY MMDD>/   the summary minutes
```

## Two passes per meeting

Those documents arrive at different times, so each meeting is digested twice:

| Pass | Source | Answers |
|---|---|---|
| `preview` | agenda + packet | what council is about to consider |
| `outcome` | minutes (~a week later) | what council actually did |

The outcome pass reads the minutes — which carry motions, movers, vote tallies,
and named dissents (`5-1, Gartin dissenting`) — and reports decisions in terms
of what each item *was*, using the preview's item summaries as context. Those
are archived as JSON when the preview runs (`archive.py`), so the outcome never
re-summarizes the packet to rediscover something already computed. If the
archive is missing, the outcome is still written from the minutes alone.

The two passes are tracked separately in state: a meeting whose preview is done
still has an outcome pending until its minutes appear. `--phase` runs just one.

A run then:

1. **Discovers** meetings by date, pairing each meeting's folders across the
   three trees (`meetings.py`). Any side may be missing. Discovery lists
   folders only; a meeting's documents are listed lazily, after the date and
   digest-state filters, so a routine poll that finds nothing new costs 9
   requests rather than one per meeting per tree — the difference between
   frequent polling being reasonable to point at a city government's server
   and not. Six of those nine only walk the fixed folder hierarchy
   (`Clerk Files` → tree → year), which changes at most yearly and is the
   obvious next thing to cache if the schedule gets tighter still. Two
   folder-naming variants appear in the archive and are both handled:
   multi-day meetings (`2025 02040506` — February 4, 5, and 6, keyed to the
   first day) and labeled special sessions (`2026 0324 Tax Levy`), which can
   fall on the same date as that day's regular meeting and so are tracked as
   distinct meetings.
2. **Selects** the passes each meeting still needs — preview, outcome, or both
   — based on what's published and what state already records.
3. **Maps** (preview): fetches every packet item PDF concurrently, extracts its
   text (`pdftext.py`), and asks the model for 2–4 sentences plus a
   significance rating of `routine` / `notable` / `major` (`summarize.py`).
4. **Reduces** (preview): hands the agenda plus every item summary to one final
   call that writes the reader-facing digest, and archives the item summaries
   for the outcome pass.
5. **Reports** (outcome): one call over the minutes plus the archived item
   summaries, producing decisions, votes, and what was deferred.
6. **Renders** Markdown, HTML, and plain text (`render.py`), then hands them to
   the configured delivery sinks (`delivery.py`).
7. **Records** the pass in a state file so the next run skips it.

A typical regular meeting is ~40 packet items and ~600 pages, measured at ~294k
input / 33k output tokens. On zen's `claude-sonnet-4-5` ($3/$15 per M) that is
roughly **$1.40 per meeting**, or ~$55/year across ~40 meetings. Pointing
`AMES_ITEM_MODEL` at `claude-haiku-4-5` ($1/$5) cuts it to about a third, since
per-item summaries are ~95% of the spend.

Items whose PDFs are scans with no text layer are reported by title in the
digest's appendix rather than silently dropped — there is no OCR in the image.

## Delivery

Delivery is behind a one-method interface so how these summaries reach a reader
stays a configuration choice. `AMES_DELIVERY` names the active sinks:

| Sink | Needs | Behavior |
|---|---|---|
| `file` | — | writes `<meeting>.md` and `<meeting>.html` to `AMES_OUTPUT_DIR` |
| `stdout` | — | prints the Markdown digest |
| `ntfy` | `NTFY_URL`, `NTFY_TOPIC` | pushes to an ntfy topic |
| `smtp` | `SMTP_HOST`, `MAIL_FROM`, `MAIL_TO` | sends a multipart HTML email |

Adding another (a transactional email API, a mailing list, a webhook) means
writing one class and registering it in `delivery.py`'s `SINKS`.

## Reading the digests

The `file` sink also regenerates `index.html` — a landing page listing meetings
newest first, each with links to both passes ("Before the meeting" / "What
council decided") as they become available. It is rebuilt by scanning the
output directory rather than tracked in state, so a digest restored from backup
or written before the index existed still appears. Titles are read back out of
each digest's `<title>`, so the index cannot drift from the pages it links.

An nginx Deployment (`kubernetes/apps/civic/ames-council-digest/web-*.yaml`)
mounts the same PVC read-only and serves that directory at
`council.mpdavis.com`, behind Authentik forward-auth. Markdown is served as
`text/plain` so it renders in the browser instead of downloading.

`ames-digest index` rebuilds the page on demand without any model calls —
useful to bootstrap a fresh volume or after moving files around.

## Configuration

Everything is environment-driven; nothing needs a rebuild to change.

| Variable | Default | Purpose |
|---|---|---|
| `AMES_WEBLINK_BASE_URL` | `https://publicdocs.cityofames.org/WebLink` | repository root |
| `AMES_WEBLINK_REPO` | `COA` | Laserfiche repository name |
| `AMES_ROOT_FOLDER_ID` | `236500` | the `Clerk Files` folder |
| `AMES_BOARD` | `City Council` | board or commission to track |
| `AMES_LLM_BASE_URL` | `https://opencode.ai/zen/v1` | Anthropic-compatible gateway |
| `AMES_LLM_API_KEY` | — | gateway key (or `OPENCODE_API_KEY`) |
| `AMES_ITEM_MODEL` | `claude-sonnet-4-5` | model for per-item summaries |
| `AMES_DIGEST_MODEL` | `claude-sonnet-4-5` | model for the final digest |
| `AMES_ITEM_CHAR_BUDGET` | `120000` | extracted characters sent per item |
| `AMES_AGENDA_CHAR_BUDGET` | `200000` | extracted characters sent for the agenda |
| `AMES_MINUTES_CHAR_BUDGET` | `200000` | extracted characters sent for the minutes |
| `AMES_MAX_CONCURRENCY` | `4` | parallel item summaries |
| `AMES_STATE_DIR` | `/data/state` | where `processed.json` lives |
| `AMES_OUTPUT_DIR` | `/data/digests` | where the `file` sink writes |
| `AMES_DELIVERY` | `file,stdout` | comma-separated sink names |

Per-item summaries dominate token spend, so `AMES_ITEM_MODEL` is worth pointing
at something cheap; `AMES_DIGEST_MODEL` is a single call and worth spending on.
Any gateway speaking the Anthropic Messages API works — `api.anthropic.com` or
a local bridge just needs a different `AMES_LLM_BASE_URL`.

Model IDs are the bare form zen's API uses (`claude-sonnet-4-5`,
`claude-haiku-4-5`, `claude-opus-5`) — not the `opencode/`-prefixed names in
zen's docs, which are its config-file form. `GET /zen/v1/models` lists them, and
an unsupported ID fails fast with a `ModelError` rather than retrying.

## Usage

```bash
# What meetings exist, what's published, and which passes are done?
ames-digest list

# Rebuild the web server's index page from what's on disk
ames-digest index

# Digest anything outstanding from the last 30 days (the CronJob's behavior)
ames-digest run

# Only report outcomes — useful to catch up on minutes without touching packets
ames-digest run --phase outcome

# One specific meeting, printed, without touching state
ames-digest run --meeting 2026-07-28 --no-state --delivery stdout

# Check document availability and text extraction without spending tokens
ames-digest run --meeting 2026-07-28 --dry-run

# A different board (no packet tree exists, so agenda + minutes only)
ames-digest --board "Parks and Recreation" run
```

`run` is the default subcommand. `--limit` (default 3) caps how many *digests* a
single run produces — a meeting needing both passes counts as two — so a first
run against a long backlog doesn't spend the whole year's budget at once.

## Local development

```bash
uv venv && uv pip install -e .
AMES_STATE_DIR=./state AMES_OUTPUT_DIR=./digests \
AMES_LLM_API_KEY=sk-... \
  python -m ames_digest run --meeting 2026-07-28 --no-state
```
