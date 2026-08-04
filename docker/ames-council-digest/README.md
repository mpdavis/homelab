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

Documents live under `Clerk Files` (folder `236500`) in parallel trees:

```
Clerk Files/Agendas/City Council/<Year>/<YYYY MMDD>/   the agenda PDF
Clerk Files/Council Packet/<Year>/<YYYY MMDD>/         one PDF per agenda item
```

A run then:

1. **Discovers** meetings by date, pairing each agenda folder with its packet
   folder (`meetings.py`). Either side may be missing — packets are often
   posted after the agenda.
2. **Maps**: fetches every packet item PDF concurrently, extracts its text
   (`pdftext.py`), and asks the model for 2–4 sentences plus a significance
   rating of `routine` / `notable` / `major` (`summarize.py`).
3. **Reduces**: hands the agenda plus every item summary to one final call that
   writes the reader-facing digest.
4. **Renders** Markdown, HTML, and plain text (`render.py`), then hands them to
   the configured delivery sinks (`delivery.py`).
5. **Records** the meeting in a state file so the next run skips it.

A typical regular meeting is ~40 packet items and ~600 pages, costing roughly
300k input / 36k output tokens.

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
| `AMES_MAX_CONCURRENCY` | `4` | parallel item summaries |
| `AMES_STATE_DIR` | `/data/state` | where `processed.json` lives |
| `AMES_OUTPUT_DIR` | `/data/digests` | where the `file` sink writes |
| `AMES_DELIVERY` | `file,stdout` | comma-separated sink names |

Per-item summaries dominate token spend, so `AMES_ITEM_MODEL` is worth pointing
at something cheap; `AMES_DIGEST_MODEL` is a single call and worth spending on.
Any gateway speaking the Anthropic Messages API works — `api.anthropic.com` or
a local bridge just needs a different `AMES_LLM_BASE_URL`.

## Usage

```bash
# What meetings exist, and which have been digested?
ames-digest list

# Digest anything new from the last 30 days (the CronJob's behavior)
ames-digest run

# One specific meeting, printed, without touching state
ames-digest run --meeting 2026-07-28 --no-state --delivery stdout

# Check document availability and text extraction without spending tokens
ames-digest run --meeting 2026-07-28 --dry-run

# A different board (no packet tree exists, so agenda only)
ames-digest --board "Parks and Recreation" run
```

`run` is the default subcommand. `--limit` (default 3) caps how many meetings a
single run will digest, so a first run against a long backlog doesn't spend the
whole year's budget at once.

## Local development

```bash
uv venv && uv pip install -e .
AMES_STATE_DIR=./state AMES_OUTPUT_DIR=./digests \
AMES_LLM_API_KEY=sk-... \
  python -m ames_digest run --meeting 2026-07-28 --no-state
```
