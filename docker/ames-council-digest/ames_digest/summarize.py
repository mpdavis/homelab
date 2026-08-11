"""Map-reduce summarization of a council meeting.

Map: every packet item PDF is fetched, text-extracted, and reduced to a few
sentences plus a significance rating. Items run concurrently because each is an
independent network round trip followed by an independent model call.

Reduce: the agenda (which supplies the meeting's structure — consent agenda,
public hearings, ordinances) and every item summary are handed to one final
call that writes the reader-facing digest.

Update: once the minutes are published, a third call reads them against that
same page and returns what council did to each of its bullets. The page is
edited in place rather than rewritten — see :mod:`ames_digest.merge`.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime

from . import merge
from .archive import PreviewArchive
from .config import Config
from .llm import LLMClient, LLMError, Usage
from .meetings import Meeting
from .pdftext import extract
from .state import PHASE_OUTCOME, PHASE_PREVIEW
from .weblink import Entry, WebLinkClient

log = logging.getLogger(__name__)

SIGNIFICANCE_LEVELS = ("routine", "notable", "major")

# Bounds on model-supplied strings that land in the archive. These are not
# validation so much as blast radius: the fields below are rendered into fixed
# furniture — a grid cell, a mono kicker — where a runaway value would break the
# layout rather than merely read badly.
MAX_FACTS = 3
MAX_FACT_CHARS = 60
MAX_SHORT_CHARS = 80

ITEM_SYSTEM = """\
You summarize agenda-item documents for a city council meeting in Ames, Iowa, \
for an engaged resident who has not read the packet.

Rules:
- Report only what the document states. Never speculate, and never invent \
dollar amounts, dates, vote counts, or names.
- Lead with what the council is being asked to approve, deny, set, or receive.
- Include concrete specifics when the document has them: dollar amounts, \
addresses, contractor or applicant names, deadlines, vote requirements.
- If the document is a routine formality (claims, minutes, license renewals, \
change orders under normal thresholds), say so plainly and keep it to one line.
- Write plainly. No marketing language, no editorializing about whether \
something is good or bad.
- Every field is independent prose. Do not repeat the summary's sentences in \
"why_it_matters", and do not restate the staff recommendation in the summary.
- A field the document does not support is an empty string. An empty field is \
always correct; a guessed one is not.

Respond with a single JSON object and nothing else:
{"summary": "2-4 sentences, or 1 sentence if routine",
 "significance": "routine" | "notable" | "major",
 "amount": "the headline dollar figure as a short string, or null",
 "item_number": "the agenda item number as printed, e.g. \\"14\\" or \\"27a\\", \
or an empty string",
 "item_type": "what kind of action this is, in title case: \\"Resolution\\", \
\\"Ordinance, second reading\\", \\"Public hearing\\", \\"Motion\\", \
\\"Consent\\", \\"Staff report\\", or whatever the document itself calls it",
 "why_it_matters": "1-2 sentences on the consequence for residents — what \
changes, who is affected, what it costs them. Empty string for routine items",
 "staff_recommendation": "one sentence stating what staff recommends council \
do, in staff's own terms, or an empty string if the document makes none",
 "facts": [{"label": "2-3 words, title case", "value": "a short phrase"}],
 "source_page": "the page number printed on the document where this item \
begins, as a string, or an empty string if the document shows none"}

Significance guidance:
- "routine": consent-agenda housekeeping with no policy choice.
- "notable": real money, a policy decision, or something residents would \
notice — contracts, rezonings, fee changes, new programs.
- "major": large spending, tax or utility rate changes, major land use \
decisions, or anything contested or precedent-setting.

"facts" guidance:
- At most three, chosen for this item rather than from a fixed list. They fill \
a small metadata grid read at a glance, so both halves stay short.
- Good labels: "Affects", "Cost to city", "Location", "Applicant", \
"Effective", "Term". Good values: "Ward 3 residents", "$1.2M over 5 years", \
"321 State Ave".
- Omit a fact rather than padding it with a vague value. An empty list is fine.
"""

DIGEST_SYSTEM = """\
You write a short email digest of an Ames, Iowa city council meeting for a \
resident who wants to know what their council is doing without reading a \
300-page packet.

Rules:
- Ground every statement in the supplied agenda and item summaries. Never add \
facts that are not there.
- Be concise and concrete. Dollar amounts, addresses, and dates earn their space; \
adjectives do not.
- Do not editorialize or take a position on any item.
- Refer to items by their agenda-item title, not by their code.

Produce GitHub-flavored Markdown with exactly these sections:

## Notable Topics
3-5 bullets, one sentence each, on what this meeting is actually about. No \
bolded labels and no sub-bullets — this is the at-a-glance read.

## Additional Reading
3-6 items covering the major and notable ones. Write each as a Markdown list \
item — a "- " marker, then a bolded short label, then a sentence or two of \
substance including the money. Exactly like this:

- **Water rate increase** — Staff recommends a 6% increase to residential \
water rates, raising the average bill by $3.40/month.

If there are fewer than three non-routine items, use however many exist.
Give every item a distinct label. After the meeting, what council decided is \
attached to each one by its label, so two items sharing one label lose an \
outcome between them.

## Public input
Public hearings, comment periods, and anything else where a resident could \
show up and speak, with dates if given. Write "None scheduled." if there are none.

## Everything else
One compact paragraph characterizing the routine consent-agenda items in \
aggregate. Do not list them individually.

Do not add a title, a greeting, a sign-off, or any section beyond these four.
"""

# Shared by both post-meeting prompts: the same reporting discipline applies
# whether the outcomes are being spliced into an existing page or written onto a
# fresh one.
_MINUTES_RULES = """\
- Report only what the minutes state. Never infer a vote, an amount, or an \
outcome that isn't written there.
- Votes matter: give the tally and name dissenters when the minutes do \
("approved 5-1, Gartin dissenting"). A unanimous vote can just say unanimous.
- Distinguish what was decided from what was merely discussed, referred to \
staff, continued to a later date, or pulled from the consent agenda.
- Note when council changed something before approving it, or departed from \
the staff recommendation.
- Do not editorialize or characterize any decision as good or bad."""

# The normal post-meeting path. The page already exists and is already correct
# about what each item *was*; the only thing missing is what happened to it. So
# this returns outcomes keyed to the page's bullets rather than a new page —
# regenerating the prose would pay twice for text nobody asked to change, and
# invite the model to quietly reword it.
UPDATE_SYSTEM = """\
You report what an Ames, Iowa city council meeting actually decided, so that \
the page written before the meeting can be updated in place.

You are given that page, the label of each bullet in its "Additional Reading" \
section, and the official summary minutes. The minutes are the authority on \
what happened; the page only tells you what each item was about, so you can \
describe outcomes in plain terms instead of quoting motion numbers.

Rules:
{rules}
- If the minutes do not cover a bullet at all, give its outcome as exactly \
"Not recorded in the minutes." Do not guess, and do not pad.
- Each update is one or two sentences, read immediately after the bullet it \
belongs to. Do not restate what the item was — say what happened to it.

Respond with a single JSON object and nothing else:
{{"updates": [{{"label": "one of the labels you were given, copied exactly",
              "outcome": "1-2 sentences: what council did, and the vote"}}],
 "after_the_meeting": "Markdown bullets covering substantive business the page \
did not anticipate: items raised at public forum, council referrals, staff \
reports, anything pulled out of the consent agenda and handled separately. \
This is often where the real news is. Use an empty string if there is none."}}

Return exactly one entry per label, in the order the labels were given. A label \
you did not receive is dropped, so copy them character for character.
"""

# The fallback: minutes exist but no preview page does, because the packet was
# never digested. One call has to produce both the page and its outcomes, so the
# outcomes ride along on a plain-text marker that render-time turns red — asking
# for raw HTML here would be a coin flip.
MINUTES_ONLY_SYSTEM = """\
You write the page for an Ames, Iowa city council meeting that has already \
happened, working from the official summary minutes alone — no pre-meeting \
packet summary exists for this meeting.

Rules:
{rules}

Produce GitHub-flavored Markdown with exactly these sections:

## Notable Topics
3-5 bullets, one sentence each, on what this meeting was about. No bolded \
labels — this is the at-a-glance read.

## Additional Reading
3-6 bullets on the items that mattered. Each bullet starts with a bolded short \
label, then a sentence on what the item was, then the marker {sentinel} \
followed by what council did and the vote. Exactly like this:

- **Water rate increase** — A 6% increase to residential water rates, the \
first since 2023. {sentinel} Approved 5-1, Gartin dissenting.

Give every bullet a distinct label, and put {sentinel} in every bullet.

## After the meeting
Items continued, tabled, referred to staff, or pulled from the consent agenda, \
and anything brought up at public forum. Write "Nothing of note." if there is \
none.

## Everything else
One compact sentence on the routine items approved as a block.

Do not add a title, a greeting, a sign-off, or any section beyond these four.
"""

UPDATE_SYSTEM = UPDATE_SYSTEM.format(rules=_MINUTES_RULES)
MINUTES_ONLY_SYSTEM = MINUTES_ONLY_SYSTEM.format(
    rules=_MINUTES_RULES, sentinel=merge.SENTINEL
)


@dataclass
class ItemSummary:
    """A single packet item after fetch, extract, and summarize.

    The docket addresses these fields individually — the card's kicker, its
    "why it matters" paragraph, its 3-up metadata grid — so they are stored
    apart rather than fused into one block of prose. ``summary`` is the item's
    plain-language description and nothing else.
    """

    # --- identity, from the repository listing ------------------------------
    code: str
    title: str
    entry_id: int
    url: str
    page_count: int | None = None
    # The document's own last-modified time, carried so a later run can tell a
    # revised packet item from one it has already paid to summarize.
    last_modified: datetime | None = None

    # --- the model's reading of the document --------------------------------
    summary: str = ""
    significance: str = "routine"
    amount: str | None = None
    # The agenda's numbering ("14"), which is not `code` — that is the
    # Laserfiche filename prefix ("A001") and exists even when the agenda
    # numbers the item differently or not at all.
    item_number: str = ""
    item_type: str = ""
    why_it_matters: str = ""
    staff_recommendation: str = ""
    # Per-item labelled values for the metadata grid. A list, not fixed
    # columns: which facts are worth showing differs item to item.
    facts: list[dict[str, str]] = field(default_factory=list)
    source_page: str = ""

    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped is None and bool(self.summary)

    def to_archive(self) -> dict:
        """The whole record, which is what makes the archive the durable artifact.

        Everything the model was paid to extract is written back, not just the
        fields the current page happens to render: re-rendering must be free,
        and a field dropped here can only be recovered by re-summarizing the
        packet.

        Skipped items are archived too, and with their reason: the page's
        appendix lists every item in the packet, and that list has to survive
        the update pass intact or items would vanish from it once the minutes
        landed.
        """
        return {
            "entry_id": self.entry_id,
            "code": self.code,
            "title": self.title,
            "url": self.url,
            "page_count": self.page_count,
            "last_modified": (
                self.last_modified.isoformat() if self.last_modified else None
            ),
            "summary": self.summary,
            "significance": self.significance,
            "amount": self.amount,
            "item_number": self.item_number,
            "item_type": self.item_type,
            "why_it_matters": self.why_it_matters,
            "staff_recommendation": self.staff_recommendation,
            "facts": [dict(fact) for fact in self.facts],
            "source_page": self.source_page,
            "skipped": self.skipped,
        }

    @classmethod
    def from_archive(cls, payload: dict) -> "ItemSummary":
        amount = payload.get("amount")
        page_count = payload.get("page_count")
        return cls(
            code=str(payload.get("code") or ""),
            title=str(payload.get("title") or ""),
            entry_id=int(payload.get("entry_id") or 0),
            url=str(payload.get("url") or ""),
            page_count=int(page_count) if isinstance(page_count, int) else None,
            last_modified=_parse_timestamp(str(payload.get("last_modified") or "")),
            summary=str(payload.get("summary") or ""),
            significance=str(payload.get("significance") or "routine"),
            amount=str(amount) if amount else None,
            item_number=str(payload.get("item_number") or ""),
            item_type=str(payload.get("item_type") or ""),
            why_it_matters=str(payload.get("why_it_matters") or ""),
            staff_recommendation=str(payload.get("staff_recommendation") or ""),
            facts=_coerce_facts(payload.get("facts")),
            source_page=str(payload.get("source_page") or ""),
            skipped=str(payload["skipped"]) if payload.get("skipped") else None,
        )


@dataclass
class MeetingDigest:
    meeting: Meeting
    body_markdown: str
    # "preview" (agenda + packet, before the meeting) or "outcome" (the same
    # page, updated from the minutes afterwards). Both write the same file; the
    # kind decides what the page links to and how the footer reads.
    kind: str = PHASE_PREVIEW
    items: list[ItemSummary] = field(default_factory=list)
    agenda_url: str | None = None
    packet_url: str | None = None
    minutes_url: str | None = None
    generated_at: datetime = field(default_factory=datetime.now)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    # What the preview pass spent, carried through the archive. One page now
    # represents both passes, so a footer reporting only the update's handful of
    # tokens would understate what the page cost by an order of magnitude.
    prior_usage: Usage = field(default_factory=Usage)
    preview_generated_at: datetime | None = None

    @property
    def skipped_items(self) -> list[ItemSummary]:
        return [i for i in self.items if not i.ok]

    @property
    def is_outcome(self) -> bool:
        return self.kind == PHASE_OUTCOME

    @property
    def total_usage(self) -> Usage:
        return Usage(
            input_tokens=self.usage.input_tokens + self.prior_usage.input_tokens,
            output_tokens=self.usage.output_tokens + self.prior_usage.output_tokens,
            calls=self.usage.calls + self.prior_usage.calls,
        )


def _parse_json_object(raw: str) -> dict:
    """Pull a JSON object out of a model response that may be fenced or chatty."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


def _parse_timestamp(raw: str) -> datetime | None:
    """Read an archived ISO timestamp back, tolerating one that never wrote."""
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        log.debug("unparseable archived timestamp %r", raw)
        return None


def _text(value: object, limit: int | None = None) -> str:
    """A model-supplied string, whitespace-collapsed, or "" for anything else.

    The model occasionally answers a string field with null, a number, or a
    nested object. None of those are worth failing an item over — the docket
    renders an absent field as absent.
    """
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    text = " ".join(str(value).split())
    if text.lower() in ("null", "none", "n/a"):
        return ""
    return text[:limit].strip() if limit else text


def _coerce_facts(raw: object) -> list[dict[str, str]]:
    """The metadata grid's labelled values, dropping anything half-formed.

    Capped because this is model output that lands in the durable archive, and
    the grid it feeds shows three; a model that returns forty would be storing
    the summary a second time in list form.
    """
    if not isinstance(raw, list):
        return []
    facts = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get("label"), MAX_FACT_CHARS)
        value = _text(entry.get("value"), MAX_FACT_CHARS)
        # Half a fact renders as a labelled blank or an unlabelled orphan.
        if label and value:
            facts.append({"label": label, "value": value})
    return facts[:MAX_FACTS]


def _coerce_summary(payload: dict) -> dict:
    """Validate the model's item JSON into the fields :class:`ItemSummary` owns.

    Every field defaults rather than raises. A model that returns half an
    object should cost that item its detail, not its place on the page — the
    keys here are exactly the ``ItemSummary`` fields the model supplies, so the
    caller can apply them wholesale.
    """
    significance = _text(payload.get("significance")).lower()
    if significance not in SIGNIFICANCE_LEVELS:
        significance = "routine"

    amount = _text(payload.get("amount"), MAX_SHORT_CHARS)

    return {
        "summary": _text(payload.get("summary")),
        "significance": significance,
        # Distinct from the string fields: nothing downstream should render an
        # empty amount as a dollar sign with no figure after it.
        "amount": amount or None,
        "item_number": _text(payload.get("item_number"), MAX_SHORT_CHARS),
        "item_type": _text(payload.get("item_type"), MAX_SHORT_CHARS),
        "why_it_matters": _text(payload.get("why_it_matters")),
        "staff_recommendation": _text(payload.get("staff_recommendation")),
        "facts": _coerce_facts(payload.get("facts")),
        "source_page": _text(payload.get("source_page"), MAX_SHORT_CHARS),
    }


class MeetingSummarizer:
    def __init__(self, cfg: Config, weblink: WebLinkClient, llm: LLMClient) -> None:
        self.cfg = cfg
        self.weblink = weblink
        self.llm = llm

    # --- map ---------------------------------------------------------------

    def summarize_item(self, item: Entry) -> ItemSummary:
        result = ItemSummary(
            code=item.item_code or "",
            title=item.title,
            entry_id=item.entry_id,
            url=self.weblink.viewer_url(item.entry_id),
            page_count=item.page_count,
            last_modified=item.last_modified,
        )

        try:
            data = self.weblink.download(item.entry_id)
        except Exception as exc:  # network/HTTP failure on one item
            log.warning("item %s download failed: %s", item.entry_id, exc)
            result.skipped = f"download failed: {exc}"
            return result

        extracted = extract(data, self.cfg.item_char_budget)
        if not extracted.usable:
            # Scanned exhibits are common and expected; the item still appears
            # in the digest's skipped list with its title so nothing vanishes.
            reason = extracted.error or "no extractable text (likely a scan)"
            log.info("item %s skipped: %s", item.name[:60], reason)
            result.skipped = reason
            return result

        prompt = self._item_prompt(item, extracted.text, extracted.truncated)
        try:
            raw = self.llm.complete(
                model=self.cfg.item_model,
                system=ITEM_SYSTEM,
                prompt=prompt,
                max_tokens=1200,
            )
            fields = _coerce_summary(_parse_json_object(raw))
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            log.warning("item %s summarization failed: %s", item.entry_id, exc)
            result.skipped = f"summarization failed: {exc}"
            return result

        # `replace` over setattr so an unknown key is a TypeError here rather
        # than a field silently invented on the instance.
        return replace(result, **fields)

    def _item_prompt(self, item: Entry, text: str, truncated: bool) -> str:
        header = [f"Agenda item: {item.name}"]
        if item.description and item.description.lower() != item.title.lower():
            header.append(f"Clerk's description: {item.description}")
        if item.page_count:
            header.append(f"Document length: {item.page_count} pages")
        if truncated:
            header.append(
                "NOTE: the text below is truncated; later pages are usually "
                "attachments and exhibits."
            )
        return "\n".join(header) + "\n\n--- DOCUMENT TEXT ---\n" + text

    # --- reduce ------------------------------------------------------------

    def compose_digest(
        self, meeting: Meeting, agenda_text: str, items: list[ItemSummary]
    ) -> str:
        lines = [
            f"Meeting: {meeting.display_name}, "
            f"{meeting.meeting_date.strftime('%B %-d, %Y')}",
            "",
        ]

        if agenda_text.strip():
            lines += ["--- AGENDA ---", agenda_text.strip(), ""]

        summarized = [i for i in items if i.ok]
        if summarized:
            lines.append("--- ITEM SUMMARIES ---")
            for item in summarized:
                amount = f" [{item.amount}]" if item.amount else ""
                lines.append(
                    f"\n[{item.significance.upper()}]{amount} {item.title}\n{item.summary}"
                )

        skipped = [i for i in items if not i.ok]
        if skipped:
            lines += [
                "",
                "--- ITEMS WITH NO MACHINE-READABLE TEXT (titles only) ---",
                *(f"- {i.title}" for i in skipped),
            ]

        return self.llm.complete(
            model=self.cfg.digest_model,
            system=DIGEST_SYSTEM,
            prompt="\n".join(lines),
            max_tokens=2500,
        )

    def _meeting_header(self, meeting: Meeting) -> list[str]:
        return [
            f"Meeting: {meeting.display_name}, "
            f"{meeting.meeting_date.strftime('%B %-d, %Y')}",
            "",
        ]

    def compose_update(
        self, meeting: Meeting, minutes_text: str, preview: PreviewArchive
    ) -> str:
        """Splice what council decided into the page written before the meeting."""
        page_labels = merge.labels(preview.body)
        lines = self._meeting_header(meeting)
        lines += [
            "--- THE PAGE AS WRITTEN BEFORE THE MEETING ---",
            preview.body.strip(),
            "",
        ]

        if page_labels:
            lines.append(
                "--- ADDITIONAL READING LABELS "
                "(return exactly one update per label, copied exactly) ---"
            )
            lines += [f"{n}. {label}" for n, label in enumerate(page_labels, 1)]
            lines.append("")

        summarized = [i for i in preview.items if i.get("summary")]
        if summarized:
            # The page's bullets are prose about a handful of items; these are
            # the per-item summaries behind them, and are what lets an outcome
            # for a consent-agenda item be written at all.
            lines.append("--- WHAT EACH AGENDA ITEM WAS (from the packet) ---")
            for item in summarized:
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                summary = " ".join(str(item.get("summary") or "").split())
                amount = item.get("amount")
                tag = str(item.get("significance") or "routine").upper()
                head = f"\n[{tag}]" + (f" [{amount}]" if amount else "") + f" {title}"
                lines.append(head + (f"\n{summary}" if summary else ""))
            lines.append("")

        lines += ["--- OFFICIAL SUMMARY MINUTES ---", minutes_text.strip()]

        raw = self.llm.complete(
            model=self.cfg.digest_model,
            system=UPDATE_SYSTEM,
            prompt="\n".join(lines),
            max_tokens=3000,
        )
        try:
            payload = _parse_json_object(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            # Not recorded in state, so the next run retries rather than leaving
            # the page permanently stuck on its pre-meeting text.
            raise RuntimeError(f"update pass returned no usable JSON: {exc}") from exc

        updates = {
            str(entry.get("label") or ""): str(entry.get("outcome") or "")
            for entry in payload.get("updates") or []
            if isinstance(entry, dict)
        }
        matched, total, body = merge.apply_updates(preview.body, updates)
        if not total:
            # The failure this guard used to skip: `total` is falsy exactly when
            # the splice did nothing at all, so the one case worth shouting about
            # was the one case that logged nothing. The page still gets its
            # "After the meeting" section, which is why it looks fine until you
            # notice no bullet carries an outcome.
            log.warning(
                "%s: no %r bullets found in the preview page — it keeps its "
                "pre-meeting text and every outcome lands in %r instead",
                meeting.key,
                merge.ADDITIONAL_READING,
                merge.AFTER_THE_MEETING,
            )
        elif matched < total:
            # Every bullet still gets an update line; the unmatched ones just say
            # "not recorded", which is indistinguishable on the page from a real
            # silence in the minutes. Worth seeing in the logs.
            log.warning(
                "%s: only %d of %d bullets matched an outcome by label",
                meeting.key,
                matched,
                total,
            )

        return merge.append_section(
            body, merge.AFTER_THE_MEETING, str(payload.get("after_the_meeting") or "")
        )

    def compose_minutes_only(self, meeting: Meeting, minutes_text: str) -> str:
        """Write the whole page from the minutes, for a meeting never previewed."""
        lines = self._meeting_header(meeting)
        lines += ["--- OFFICIAL SUMMARY MINUTES ---", minutes_text.strip()]
        raw = self.llm.complete(
            model=self.cfg.digest_model,
            system=MINUTES_ONLY_SYSTEM,
            prompt="\n".join(lines),
            max_tokens=2500,
        )
        return merge.promote_sentinels(raw)

    # --- orchestration -----------------------------------------------------

    def _usage_snapshot(self) -> Usage:
        # The LLM client's counters run for the whole process, so callers
        # snapshot and diff — otherwise the second digest in a run would claim
        # the first one's tokens too.
        return Usage(
            input_tokens=self.llm.usage.input_tokens,
            output_tokens=self.llm.usage.output_tokens,
            calls=self.llm.usage.calls,
        )

    def _usage_since(self, before: Usage) -> Usage:
        return Usage(
            input_tokens=self.llm.usage.input_tokens - before.input_tokens,
            output_tokens=self.llm.usage.output_tokens - before.output_tokens,
            calls=self.llm.usage.calls - before.calls,
        )

    def run_preview(self, meeting: Meeting) -> MeetingDigest:
        """Digest the agenda and packet — what council is about to consider."""
        before = self._usage_snapshot()

        agenda_text = ""
        agenda_url = None
        if meeting.agenda:
            agenda_url = self.weblink.viewer_url(meeting.agenda.entry_id)
            try:
                agenda_pdf = self.weblink.download(meeting.agenda.entry_id)
                agenda_text = extract(agenda_pdf, self.cfg.agenda_char_budget).text
            except Exception as exc:
                log.warning("agenda download/extract failed: %s", exc)

        items: list[ItemSummary] = []
        if meeting.packet_items:
            log.info(
                "summarizing %d packet items (concurrency %d)",
                len(meeting.packet_items),
                self.cfg.max_concurrency,
            )
            with ThreadPoolExecutor(max_workers=self.cfg.max_concurrency) as pool:
                items = list(pool.map(self.summarize_item, meeting.packet_items))
        elif meeting.packet_master:
            # No per-item breakdown published — fall back to the combined packet
            # so the meeting still gets substance rather than agenda titles.
            log.info("no individual items; falling back to the combined packet")
            items = [self.summarize_item(meeting.packet_master)]

        if not agenda_text.strip() and not any(i.ok for i in items):
            raise RuntimeError(
                f"no usable text for {meeting.key}: agenda and packet both "
                "unreadable or absent"
            )

        body = self.compose_digest(meeting, agenda_text, items)

        return MeetingDigest(
            meeting=meeting,
            body_markdown=body,
            kind=PHASE_PREVIEW,
            items=items,
            agenda_url=agenda_url,
            packet_url=(
                self.weblink.viewer_url(meeting.packet_master.entry_id)
                if meeting.packet_master
                else None
            ),
            usage=self._usage_since(before),
            model=self.cfg.digest_model,
        )

    def run_outcome(
        self, meeting: Meeting, preview: PreviewArchive
    ) -> MeetingDigest:
        """Update the meeting's page with what council actually did.

        This produces the same page the preview did, so it overwrites it rather
        than sitting beside it. An archive with no page body — none was ever
        written, or it predates the merged format — falls back to writing the
        page from the minutes alone.
        """
        if not meeting.minutes:
            raise RuntimeError(f"no minutes published for {meeting.key}")

        before = self._usage_snapshot()

        minutes_pdf = self.weblink.download(meeting.minutes.entry_id)
        extracted = extract(minutes_pdf, self.cfg.minutes_char_budget)
        if not extracted.usable:
            raise RuntimeError(
                f"minutes for {meeting.key} have no usable text: "
                f"{extracted.error or 'likely a scan'}"
            )

        if preview.has_page:
            body = self.compose_update(meeting, extracted.text, preview)
            items = [ItemSummary.from_archive(i) for i in preview.items]
        else:
            log.info(
                "%s has no archived page; writing it from the minutes alone",
                meeting.key,
            )
            body = self.compose_minutes_only(meeting, extracted.text)
            items = []

        return MeetingDigest(
            meeting=meeting,
            body_markdown=body,
            kind=PHASE_OUTCOME,
            items=items,
            agenda_url=(
                self.weblink.viewer_url(meeting.agenda.entry_id)
                if meeting.agenda
                else None
            ),
            packet_url=(
                self.weblink.viewer_url(meeting.packet_master.entry_id)
                if meeting.packet_master
                else None
            ),
            minutes_url=self.weblink.viewer_url(meeting.minutes.entry_id),
            usage=self._usage_since(before),
            model=self.cfg.digest_model,
            prior_usage=preview.usage,
            preview_generated_at=_parse_timestamp(preview.generated_at),
        )
