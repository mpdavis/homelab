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

## One page per meeting, in two passes

Those documents arrive at different times, so each meeting is digested twice —
but both passes write the **same page**. A meeting has one URL, whose content
grows once the minutes land:

| Pass | Source | What it does to the page |
|---|---|---|
| `preview` | agenda + packet | writes it: what council is about to consider |
| `outcome` | minutes (~a week later) | updates it in place with what council did |

The page itself:

```
# City Council — July 28, 2026
6:00 PM · City Hall, 515 Clark Ave · Minutes · Agenda · Full packet

## Notable Topics          bullets — the at-a-glance read
## Additional Reading      the major and notable items, one bullet each
## Public input            hearings and comment periods
## Everything else         the consent agenda in aggregate
## After the meeting       added by the outcome pass, for business the packet
                           did not anticipate — public forum, referrals
### Every item in this packet
### On the agenda, with no packet document
```

The outcome pass reads the minutes — which carry motions, movers, vote tallies,
and named dissents (`5-1, Gartin dissenting`) — and returns one update per
**Additional Reading** bullet. Each is appended to its bullet in red, so the
forecast and the record are distinguishable at a glance:

> - **Water rate increase** — Staff recommends a 6% increase to residential
>   water rates, raising the average bill by $3.40/month.
>   <span style="color:#b42318">**Update:** Approved 5-1, Gartin dissenting.</span>

Bullets are matched by their bolded label, not by position: `merge.py` extracts
the labels, the model must echo them back, and a label it reorders or invents
fails to match and leaves that bullet reading `Not recorded in the minutes` —
rather than stapling one item's vote onto another. A bullet the minutes are
genuinely silent about gets that same line, which is honest: silence is a real
outcome when an item is pulled, deferred, or never reached.

The page's Markdown, the segmented agenda, and the per-item records behind them
are archived as JSON when the preview runs (`archive.py`). Each delivered
meeting also gets a versioned `<meeting>.json` record beside its Markdown and
HTML; that record is the durable artifact and retains both the preview and
outcome bodies, so the outcome pass
edits prose that is already correct instead of paying to regenerate it — and
never re-summarizes the packet, or re-segments the agenda, to rediscover
something already computed. If the archive has no page (no preview ever ran, or
it predates this format), the outcome pass writes the whole page from the
minutes alone.

Each archived item is stored whole rather than trimmed to what today's page
renders, and carries the `entry_id` and `last_modified` of the document it was
read from. That makes the archive the answer to "what did we summarize, and from
which version of the document" — the question that matters because the clerk
revises packet documents in place after we have already digested them.

## A packet is not final when it first appears

The clerk uploads a packet over an hour or so and then **revises documents in
place**, sometimes the next day, hours before the meeting. Digesting once and
never looking again means a run that lands mid-upload captures a partial packet
permanently.

This is not hypothetical. On the 2026-08-11 packet, all 33 documents were
created on 8/10 between 2:06 and 3:13 PM, and four were revised the next day:

| Document | Created | Last modified |
|---|---|---|
| `~Master` | 8/10 2:06 PM | 8/10 2:06 PM |
| `A002 - payment of claims` | 8/10 2:13 PM | **8/11 2:12 PM** |
| `A003 - Report of Change Orders` | 8/10 2:15 PM | **8/11 2:15 PM** |
| `A004 - Summary of Minutes` | 8/10 2:16 PM | **8/11 2:13 PM** |

**The item count never changed**, so a "did the count change?" check misses this
entirely. It has to be timestamps. The same happens on the outcome side: the
7/28 minutes folder received `~Master` at 8/7 2:48 PM and `A001` six and a half
hours later, so a run in that window sees half the minutes.

Detection runs in three layers, cheapest first:

| Layer | Cost | What it answers |
|---|---|---|
| **Folder fingerprint** | free, every poll | has anything in this meeting moved? |
| **Manifest diff** | one listing, only on a layer-1 miss | *which* documents moved? |
| **Policy** | free | are we willing to act on it? |

Layer 1 is free because Laserfiche propagates a folder's `LastModified` up from
its children, and discovery already lists the year folders those rows come from.
A no-op poll therefore still costs **nine requests** — the figure the discovery
section above is careful about — and that is asserted against the live
repository, not just assumed.

Because layer 1 costs nothing, it is allowed to be over-sensitive. A folder
touched with no surviving change to any document inside it fails layer 1, costs
exactly one listing, is re-baselined so it stops recurring, and never reaches a
model call.

Comparisons are on the **raw timestamp strings**, never parsed ordering. The
repository serves naive local wall time, so string inequality sidesteps DST and
clock skew and still catches a stamp moving backwards. Only the quiet period
below needs a real clock.

### Only the changed documents are re-summarized

A full packet is ~$1.40; re-reading all 33 documents to pick up four changes
would make update detection correct but too expensive to leave on. So a revision
reuses the archived summary for every document whose timestamp has not moved,
re-summarizes only the ones that have, drops the ones that are gone, and re-runs
the single reduce call over the merged set. The agenda outline is reused on the
same terms — if the agenda PDF itself is unchanged, segmentation is not bought
again. Matching always re-runs, because it is pure, free, and the item set it
has to place may have changed.

That turns the 8/11 revision from 33 item calls into 4.

Token counts **accumulate** rather than replace: a revision that re-summarized
four of 33 items spent a fraction of the original, and storing that fraction
would make the index's cumulative ledger fall after a revision.

### Spend guardrails

Detection and cheap re-summarization make revisions possible and affordable.
These decide when we act, so a CronJob polling every ten minutes cannot rework
itself into a large bill:

| Guardrail | Default | Why |
|---|---|---|
| **Quiet period** | 2 hours | The 8/11 packet uploaded over 67 minutes. Digesting mid-burst guarantees rework, so a meeting's folders must sit still first. |
| **Freeze** | — | A preview freezes once its meeting has happened; the whole meeting freezes once its outcome publishes. A clerk tidying old folders must not re-bill us for history nobody is re-reading. |
| **Revision cap** | 5 per pass | A runaway stops loudly, naming the meeting in a warning, rather than silently. |
| **`--since-days`** | 30 | Revisions to meetings older than the window go undetected. That is a deliberate bound, not an oversight. |

`--recheck` re-evaluates freshness for passes the policy would leave alone —
frozen meetings, ones at the cap — while still only re-digesting what actually
changed. That is the difference from `--force`, which re-digests regardless and
pays full price.

The quiet period is the one piece that needs a real clock, since it measures a
stamp's *age* rather than its identity. Listing timestamps carry no timezone, so
`AMES_REPO_TIMEZONE` (default `America/Chicago`) says which clock wrote them. A
misconfigured zone degrades the quiet period rather than breaking the run:
unreadable or future-dated stamps are treated as settled, and a future stamp
logs a warning pointing at the timezone.

A page rebuilt from revised sources says so in its footer.

The two passes are tracked separately in state: a meeting whose preview is done
still has an outcome pending until its minutes appear. `--phase` runs just one.

Each meeting has one rendered URL. The outcome pass enriches that page in place;
the JSON record retains the preview body for later template work.

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
   text (`pdftext.py`), and asks the model for a structured record of the item
   (`summarize.py`): a 2–4 sentence summary, a significance rating of
   `routine` / `notable` / `major`, the agenda number and item type, why it
   matters, the staff recommendation, a short list of labelled facts, and the
   source page. The fields are stored apart rather than fused into one block of
   prose, because the page addresses each of them individually.
4. **Segments** (preview): one call over the agenda PDF returns the meeting's
   own structure as data (`agenda.py`) — see below.
5. **Reduces** (preview): hands the segmented agenda plus every item summary to
   one final call that writes the reader-facing page, and archives that page,
   the outline, and the full item records for the outcome pass.
6. **Updates** (outcome): one call over the minutes plus the archived page,
   returning an outcome and vote per Additional Reading bullet, which `merge.py`
   splices into the page (`summarize.py`). The page is rewritten in place.
7. **Renders** Markdown, HTML, and plain text (`render.py`), then hands them to
   the configured delivery sinks (`delivery.py`).
8. **Records** the pass in a state file so the next run skips it.

## The agenda is the meeting's structure

The packet is a bag of PDFs named by Laserfiche code — `A001`, `A002` — which
is a filename artifact, not the agenda's numbering, and sorting by it is not the
order council takes items up. Everything structural lives in the agenda PDF and
nowhere else: the printed item numbers, the section headings, which items ride
the consent agenda, and the meeting's time and place. Until it was segmented,
that PDF was dumped into the reduce prompt as raw text, so none of it survived
as data.

One call per meeting now reads the agenda and returns it as records —
`{item_number, title, item_type, section}` in printed order, plus the time and
location for the page header. A pure string-similarity pass then joins that
outline to the packet (`agenda.py`), scoring each pair on token overlap and
character similarity, taking the best-scoring pairs first, and assigning
one-to-one.

The join has to be fuzzy, and both failure directions are real. On the
2026-07-28 meeting — 40 agenda entries, 39 packet PDFs — the codes are offset by
one (item 1 is a presentation with no document), `A005` does not exist, and the
same water-monitoring agreement was uploaded twice as `A018` and `A019` against
a single agenda entry. Neither side is a permutation of the other, so nothing is
resolved by dropping it:

| | Behavior |
|---|---|
| Agenda entry with no packet PDF | stays in the outline as an orphan, listed under **On the agenda, with no packet document** — proclamations, public forum, and council referrals never produce a document |
| Packet PDF with no agenda entry | keeps its full record and lands after the ordered items, still linked in the packet appendix |

The match rate is logged every run (`agenda match: 38 of 40 …`). If segmentation
fails or the agenda is missing entirely, the run degrades rather than aborts:
items keep their Laserfiche order and take their weight from significance alone.

### Weight

Card treatment is driven by `weight`, which is **not** a rename of
`significance`. Significance has three values and is the model's opinion of a
document; weight has four and is a structural property of the meeting, derived
once from significance plus the agenda's own sectioning and then persisted:

| `significance` | on the consent agenda | elsewhere |
|---|---|---|
| `major` | `consent` | `major` |
| `notable` | `consent` | `standard` |
| `routine` | `consent` | `routine` |

Consent membership outranks significance — an item passed in a block without
discussion reads as one line however interesting its document is. It is derived
at digest time and never recomputed at render time, so a page re-rendered a year
later looks the way it did. There is deliberately no override file.

A typical regular meeting is ~40 packet items and ~600 pages, measured at ~294k
input / 33k output tokens. On zen's `claude-sonnet-4-5` ($3/$15 per M) that is
roughly **$1.40 per meeting**, or ~$55/year across ~40 meetings. Pointing
`AMES_ITEM_MODEL` at `claude-haiku-4-5` ($1/$5) cuts it to about a third, since
per-item summaries are ~95% of the spend.

That measurement predates agenda segmentation, which adds one call over the
agenda text — ~12k characters in, an item list out. It is well inside the noise
of a 40-item packet, and it partly pays for itself: the reduce call now receives
the ordered outline instead of the raw agenda dump it used to be handed.

Items whose PDFs are scans with no text layer are reported by title in the
digest's appendix rather than silently dropped — there is no OCR in the image.

## Delivery

Delivery is behind a one-method interface so how these summaries reach a reader
stays a configuration choice. `AMES_DELIVERY` names the active sinks:

| Sink | Needs | Behavior |
|---|---|---|
| `file` | — | writes `<meeting>.md` and `<meeting>.html` to `AMES_OUTPUT_DIR`; the outcome pass overwrites both |
| `stdout` | — | prints the Markdown digest |
| `ntfy` | `NTFY_URL`, `NTFY_TOPIC` | pushes to an ntfy topic |
| `smtp` | `SMTP_HOST`, `MAIL_FROM`, `MAIL_TO` | sends a multipart HTML email |

Adding another (a transactional email API, a mailing list, a webhook) means
writing one class and registering it in `delivery.py`'s `SINKS`.

## Reading the digests

The `file` sink also regenerates `index.html` — a landing page listing meetings
newest first. It is rebuilt by scanning the output directory rather than tracked
in state, so a digest restored from backup or written before the index existed
still appears. Titles are read back out of each digest's `<title>`, so the index
cannot drift from the pages it links.

An nginx Deployment (`kubernetes/apps/civic/ames-council-digest/web-*.yaml`)
mounts the same PVC read-only and serves that directory at
`council.mpdavis.com`, behind Authentik forward-auth. Markdown is served as
`text/plain` so it renders in the browser instead of downloading.

`ames-digest index` rebuilds the page on demand without any model calls —
useful to bootstrap a fresh volume or after moving files around.

`ames-digest render` rebuilds every Markdown and HTML meeting page from the
stored JSON records, then refreshes the index. It makes template changes free:
no model client is constructed and no source documents are fetched.

### Usage counters

The index also carries a KPI row of cumulative model usage — tokens in/out,
digests produced, model calls — summed from the state file, which is the only
complete ledger. A digest's own footer reports one pass, and rendered files can
be deleted without the spend being undone.

Set both `AMES_PRICE_*_PER_MTOK` to add a spend estimate. They are unset by
default deliberately: gateway prices change and vary by model, so a figure
baked into the image would go stale while still reading as authoritative.

Records written before call tracking existed contribute tokens but no call
count. The tile is omitted until there is a real figure, and the total is
marked with `+` while any such records remain, rather than silently
undercounting.

## Configuration

Everything is environment-driven; nothing needs a rebuild to change.

| Variable | Default | Purpose |
|---|---|---|
| `AMES_WEBLINK_BASE_URL` | `https://publicdocs.cityofames.org/WebLink` | repository root |
| `AMES_WEBLINK_REPO` | `COA` | Laserfiche repository name |
| `AMES_ROOT_FOLDER_ID` | `236500` | the `Clerk Files` folder |
| `AMES_BOARD` | `City Council` | board or commission to track |
| `AMES_MEETING_TIME` | `6:00 PM` | fallback when the agenda does not print a start time |
| `AMES_MEETING_LOCATION` | `City Hall, 515 Clark Ave` | fallback when the agenda does not print a place |
| `AMES_QUIET_PERIOD_MINUTES` | `120` | how long a meeting's folders must sit unchanged before digesting; `0` disables |
| `AMES_REVISION_CAP` | `5` | most revisions to pay for on one pass |
| `AMES_REPO_TIMEZONE` | `America/Chicago` | which clock wrote the repository's timestamps (quiet period only) |
| `AMES_LLM_BASE_URL` | `https://opencode.ai/zen/v1` | Anthropic-compatible gateway |
| `AMES_LLM_API_KEY` | — | gateway key (or `OPENCODE_API_KEY`) |
| `AMES_ITEM_MODEL` | `claude-sonnet-4-5` | model for per-item summaries |
| `AMES_DIGEST_MODEL` | `claude-sonnet-4-5` | model for the final digest |
| `AMES_ITEM_CHAR_BUDGET` | `120000` | extracted characters sent per item |
| `AMES_AGENDA_CHAR_BUDGET` | `200000` | extracted characters sent for the agenda |
| `AMES_MINUTES_CHAR_BUDGET` | `200000` | extracted characters sent for the minutes |
| `AMES_PRICE_INPUT_PER_MTOK` | — | $/M input tokens; enables the spend estimate |
| `AMES_PRICE_OUTPUT_PER_MTOK` | — | $/M output tokens; enables the spend estimate |
| `AMES_MAX_CONCURRENCY` | `4` | parallel item summaries |
| `AMES_STATE_DIR` | `/data/state` | where `processed.json` lives |
| `AMES_OUTPUT_DIR` | `/data/digests` | where the `file` sink writes |
| `AMES_DELIVERY` | `file,stdout` | comma-separated sink names |

`AMES_MEETING_TIME` and `AMES_MEETING_LOCATION` belong beside `AMES_BOARD`:
change the board and these change with it, which is what makes them a per-body
fallback rather than a constant. They are used only when the agenda PDF does not
print the value itself.

Per-item summaries dominate token spend, so `AMES_ITEM_MODEL` is worth pointing
at something cheap; `AMES_DIGEST_MODEL` is a single call and worth spending on.
It now covers two calls — the agenda segmentation and the final digest — but
both are single calls over text already downloaded, against ~40 item calls.
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

# Rebuild every meeting page from JSON records, without model calls
ames-digest render

# Digest anything outstanding from the last 30 days (the CronJob's behavior)
ames-digest run

# Only report outcomes — useful to catch up on minutes without touching packets
ames-digest run --phase outcome

# One specific meeting, printed, without touching state
ames-digest run --meeting 2026-07-28 --no-state --delivery stdout

# Look for revised source documents even where policy would leave them alone.
# Still only re-digests what actually changed — unlike --force
ames-digest run --recheck

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
uv venv && uv pip install -e ".[dev]"
AMES_STATE_DIR=./state AMES_OUTPUT_DIR=./digests \
AMES_LLM_API_KEY=sk-... \
  python -m ames_digest run --meeting 2026-07-28 --no-state
```

## Tests

```bash
pytest            # from docker/ames-council-digest
```

The suite is pure: no network, no model gateway, no PDFs pulled off the city's
server. Everything expensive is stubbed at its seam — a fake HTTP client replays
canned Laserfiche listings, PDFs are generated in-process, and no test calls a
model — so the whole thing runs in well under a second.

It is a **required check** on `main`
(`.github/workflows/ames-council-digest-tests.yml`), so a red suite blocks the
merge rather than merely annotating it. That workflow deliberately carries no
`paths` filter: a required check has to report on every pull request, because
one that is skipped never reports and GitHub waits on it forever. The image
build keeps its own `paths` filter and lives separately, which is why the two
are not one workflow.

What it covers is the logic that decides things, which is where the bugs are and
where the money is:

| File | What it pins down |
|---|---|
| `test_agenda.py` | outline coercion, and the fuzzy agenda↔packet join — one-to-one assignment, boilerplate not counting as similarity, both orphan directions, determinism |
| `test_summarize.py` | weight derivation, item coercion, archive round-trip, and `apply_outline` ordering and field precedence |
| `test_merge.py` | splicing outcomes into the page — label matching, bullets the model wrote as bare paragraphs, and re-runs replacing rather than stacking |
| `test_selection.py` | which passes a run spends money on, and which meetings never cost a listing |
| `test_archive.py`, `test_state.py` | the two things whose loss costs a re-summarized packet: degrading on corrupt files, migrating v1 state, refusing unsafe keys |
| `test_weblink.py` | folder-name parsing, listing column mapping, and paging |
| `test_render.py`, `test_index.py` | what reaches the page and the landing page, including that nothing published becomes invisible |
| `test_llm.py`, `test_pdftext.py`, `test_config.py`, `test_delivery.py`, `test_meetings.py` | response parsing and the retry loop, scan detection, env parsing, sink behavior, meeting identity |

Model prompts are not tested — they are not deterministic and a test asserting
their wording would only pin the prompt to itself. What *is* tested is every
path a model response takes after it arrives, including the malformed ones.
