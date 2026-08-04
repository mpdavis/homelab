"""Map-reduce summarization of a council meeting.

Map: every packet item PDF is fetched, text-extracted, and reduced to a few
sentences plus a significance rating. Items run concurrently because each is an
independent network round trip followed by an independent model call.

Reduce: the agenda (which supplies the meeting's structure — consent agenda,
public hearings, ordinances) and every item summary are handed to one final
call that writes the reader-facing digest.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .llm import LLMClient, LLMError, Usage
from .meetings import Meeting
from .pdftext import extract
from .weblink import Entry, WebLinkClient

log = logging.getLogger(__name__)

SIGNIFICANCE_LEVELS = ("routine", "notable", "major")

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

Respond with a single JSON object and nothing else:
{"summary": "2-4 sentences, or 1 sentence if routine",
 "significance": "routine" | "notable" | "major",
 "amount": "the headline dollar figure as a short string, or null"}

Significance guidance:
- "routine": consent-agenda housekeeping with no policy choice.
- "notable": real money, a policy decision, or something residents would \
notice — contracts, rezonings, fee changes, new programs.
- "major": large spending, tax or utility rate changes, major land use \
decisions, or anything contested or precedent-setting.
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

## The short version
One paragraph, 2-4 sentences, on what this meeting is actually about.

## Worth a closer look
3-6 bullets covering the major and notable items. Each bullet starts with a \
bolded short label, then a sentence or two of substance including the money.
If there are fewer than three non-routine items, use however many exist.

## Public input
Public hearings, comment periods, and anything else where a resident could \
show up and speak, with dates if given. Write "None scheduled." if there are none.

## Everything else
One compact paragraph characterizing the routine consent-agenda items in \
aggregate. Do not list them individually.

Do not add a title, a greeting, a sign-off, or any section beyond these four.
"""


@dataclass
class ItemSummary:
    """A single packet item after fetch, extract, and summarize."""

    code: str
    title: str
    entry_id: int
    url: str
    pages: int | None = None
    summary: str = ""
    significance: str = "routine"
    amount: str | None = None
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped is None and bool(self.summary)


@dataclass
class MeetingDigest:
    meeting: Meeting
    body_markdown: str
    items: list[ItemSummary] = field(default_factory=list)
    agenda_url: str | None = None
    packet_url: str | None = None
    generated_at: datetime = field(default_factory=datetime.now)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    @property
    def skipped_items(self) -> list[ItemSummary]:
        return [i for i in self.items if not i.ok]


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


def _coerce_summary(payload: dict) -> tuple[str, str, str | None]:
    summary = str(payload.get("summary") or "").strip()
    significance = str(payload.get("significance") or "routine").strip().lower()
    if significance not in SIGNIFICANCE_LEVELS:
        significance = "routine"
    amount = payload.get("amount")
    amount = str(amount).strip() if amount not in (None, "", "null") else None
    return summary, significance, amount


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
            pages=item.page_count,
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
                max_tokens=800,
            )
            summary, significance, amount = _coerce_summary(_parse_json_object(raw))
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            log.warning("item %s summarization failed: %s", item.entry_id, exc)
            result.skipped = f"summarization failed: {exc}"
            return result

        result.summary = summary
        result.significance = significance
        result.amount = amount
        return result

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
            f"Meeting: {meeting.board}, {meeting.meeting_date.strftime('%B %-d, %Y')}",
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

    # --- orchestration -----------------------------------------------------

    def run(self, meeting: Meeting) -> MeetingDigest:
        # The LLM client's counters run for the whole process, so snapshot them
        # here and report the delta — otherwise the second meeting in a run
        # would claim the first meeting's tokens too.
        before = Usage(
            input_tokens=self.llm.usage.input_tokens,
            output_tokens=self.llm.usage.output_tokens,
            calls=self.llm.usage.calls,
        )

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
            items=items,
            agenda_url=agenda_url,
            packet_url=(
                self.weblink.viewer_url(meeting.packet_master.entry_id)
                if meeting.packet_master
                else None
            ),
            usage=Usage(
                input_tokens=self.llm.usage.input_tokens - before.input_tokens,
                output_tokens=self.llm.usage.output_tokens - before.output_tokens,
                calls=self.llm.usage.calls - before.calls,
            ),
            model=self.cfg.digest_model,
        )
