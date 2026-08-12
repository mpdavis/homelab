"""The meeting's own structure, and how it maps onto the packet.

The packet is a bag of PDFs whose filenames carry a Laserfiche code (``A001``)
rather than an agenda number, and whose natural order is that code's rather than
the order council will take them up. Everything structural — the printed item
numbering, the section headings, which items ride the consent agenda, and the
meeting's time and place — exists in the agenda PDF and nowhere else. Until now
that PDF was dumped into the reduce prompt as raw text, so none of it survived
as data.

So one model call reads the agenda and returns that outline, and a pure
string-similarity pass joins it to the packet. The join has to be fuzzy: agenda
wording and clerk filenames describe the same item in different words, and
neither side is guaranteed complete. Both failure directions are named rather
than dropped — an agenda entry with no PDF stays in the outline flagged
unmatched, and a packet PDF with no agenda entry keeps its place at the end of
the item list. Nothing published is allowed to become invisible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Protocol, Sequence

from .llm import text_field

log = logging.getLogger(__name__)

# Bounds on model output that lands in the durable archive, in the same spirit
# as summarize.py's: a runaway value breaks the layout rather than merely
# reading badly. A regular meeting agenda runs 40-odd items.
MAX_AGENDA_ITEMS = 300
MAX_TITLE_CHARS = 300
MAX_SHORT_CHARS = 80

CONSENT_RE = re.compile(r"\bconsent\b", re.IGNORECASE)

AGENDA_SYSTEM = """\
You read the printed agenda for a city council meeting in Ames, Iowa and \
return its structure. You are not summarizing anything — this is transcription \
of what the agenda itself lays out.

Rules:
- Return every numbered agenda item, in the order the agenda prints them.
- Copy each title from the agenda. Trim trailing boilerplate if it runs long, \
but never paraphrase, never reorder, and never invent an item.
- The lettered parts beneath a number (a., b., c.) are the separate actions \
council takes on that one item, not items of their own. Return the numbered \
item and leave its parts inside it.
- "section" is the agenda's own heading above the item, copied as printed \
("PRESENTATION", "CONSENT AGENDA", "ADMINISTRATION", "HEARINGS", \
"ORDINANCES"). An item under no heading gets an empty string. Getting this \
right matters most for the consent agenda, which decides how the item is \
presented to the reader.
- The headings sit in a narrow column in the printed agenda, so the text you \
are given often places a heading *after* the items it governs, or between two \
of them. Read the agenda's structure, not the order the text happens to arrive \
in: the consent agenda runs from the "CONSENT AGENDA" heading until the next \
heading, however the text is interleaved.
- Skip procedural furniture that carries no item number — call to order, roll \
call, pledge, adjournment.
- Report only what is printed. If the agenda does not state the time or the \
place, return an empty string rather than a guess.

Respond with a single JSON object and nothing else:
{"meeting_time": "the start time as printed, e.g. \\"6:00 PM\\", or an empty \
string",
 "location": "where the meeting is held, e.g. \\"City Hall, 515 Clark Ave\\", \
or an empty string",
 "items": [{"item_number": "as printed, e.g. \\"14\\" or \\"27a\\", or an \
empty string if the agenda does not number it",
            "title": "the item's text as printed, on one line",
            "item_type": "what kind of action this is, in title case: \\
\\"Resolution\\", \\"Ordinance, second reading\\", \\"Public hearing\\", \
\\"Motion\\", \\"Consent\\", \\"Staff report\\", or whatever the agenda calls it",
            "section": "the heading printed above it, or an empty string"}]}
"""


@dataclass(frozen=True)
class AgendaItem:
    """One entry in the printed agenda."""

    item_number: str = ""
    title: str = ""
    item_type: str = ""
    # The agenda's own heading above this item. Consent membership is read back
    # out of it rather than asked for as a separate flag, because the heading is
    # what the agenda actually prints and a flag would be a second thing for the
    # model to get wrong.
    section: str = ""
    # Set by :func:`match`. An entry the packet has no PDF for is not an error —
    # plenty of agenda business (proclamations, public forum, council referrals)
    # never produces a document — but it must stay visible.
    matched: bool = False

    @property
    def is_consent(self) -> bool:
        return bool(CONSENT_RE.search(self.section))

    def to_archive(self) -> dict:
        return {
            "item_number": self.item_number,
            "title": self.title,
            "item_type": self.item_type,
            "section": self.section,
            "matched": self.matched,
        }

    @classmethod
    def from_archive(cls, payload: dict) -> "AgendaItem":
        return cls(
            item_number=str(payload.get("item_number") or ""),
            title=str(payload.get("title") or ""),
            item_type=str(payload.get("item_type") or ""),
            section=str(payload.get("section") or ""),
            matched=bool(payload.get("matched")),
        )


@dataclass
class AgendaOutline:
    """The agenda as data: its items in order, and where and when it happens."""

    items: list[AgendaItem] = field(default_factory=list)
    # "6:00 PM" and "City Hall, 515 Clark Ave". Both fall back to a configured
    # per-board default when the agenda does not print them.
    meeting_time: str = ""
    location: str = ""

    @property
    def orphans(self) -> list[AgendaItem]:
        """Agenda entries no packet document was found for."""
        return [item for item in self.items if not item.matched]

    @property
    def venue(self) -> str:
        """The header's "6:00 PM · City Hall, 515 Clark Ave", or ""."""
        return " · ".join(p for p in (self.meeting_time, self.location) if p)

    def with_matches(self, matched: set[int]) -> "AgendaOutline":
        return replace(
            self,
            items=[
                replace(item, matched=n in matched) for n, item in enumerate(self.items)
            ],
        )

    def to_archive(self) -> dict:
        return {
            "meeting_time": self.meeting_time,
            "location": self.location,
            "items": [item.to_archive() for item in self.items],
        }

    @classmethod
    def from_archive(cls, payload: object) -> "AgendaOutline":
        if not isinstance(payload, dict):
            return cls()
        raw_items = payload.get("items")
        return cls(
            items=[
                AgendaItem.from_archive(i)
                for i in (raw_items if isinstance(raw_items, list) else [])
                if isinstance(i, dict)
            ],
            meeting_time=str(payload.get("meeting_time") or ""),
            location=str(payload.get("location") or ""),
        )


def coerce_outline(payload: dict) -> AgendaOutline:
    """Validate the model's agenda JSON, dropping anything half-formed.

    Like the item pass, every field defaults rather than raises: a malformed
    outline should cost the page its ordering, not its existence. An entry with
    no title at all is dropped, because a numbered blank renders as a hole.
    """
    raw_items = payload.get("items")
    items: list[AgendaItem] = []
    for entry in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(entry, dict):
            continue
        title = text_field(entry.get("title"), MAX_TITLE_CHARS)
        if not title:
            continue
        items.append(
            AgendaItem(
                item_number=text_field(entry.get("item_number"), MAX_SHORT_CHARS),
                title=title,
                item_type=text_field(entry.get("item_type"), MAX_SHORT_CHARS),
                section=text_field(entry.get("section"), MAX_SHORT_CHARS),
            )
        )

    if len(items) > MAX_AGENDA_ITEMS:
        log.warning(
            "agenda segmentation returned %d items; keeping the first %d",
            len(items),
            MAX_AGENDA_ITEMS,
        )
        items = items[:MAX_AGENDA_ITEMS]

    return AgendaOutline(
        items=items,
        meeting_time=text_field(payload.get("meeting_time"), MAX_SHORT_CHARS),
        location=text_field(payload.get("location"), MAX_SHORT_CHARS),
    )


# --- matching ---------------------------------------------------------------

# Above this, two titles are taken to describe the same item. Chosen for a
# blended score where a true pair typically lands 0.55-0.85 and an unrelated
# pair 0.15-0.35, so the gap is wide and the exact cut is not delicate.
MATCH_THRESHOLD = 0.45
# Agreement on the printed item number is strong corroboration, but it cannot
# stand alone: the packet's numbering is extracted per-document by the item
# pass, which reads it off whatever the PDF's first page happens to show.
NUMBER_BONUS = 0.15
# Weighting between the two similarity measures. Token overlap carries more
# because it survives the reordering and boilerplate that separate an agenda
# line from a clerk's filename; the sequence ratio breaks ties between items
# that share their distinctive words (two liquor licenses, two change orders).
TOKEN_WEIGHT = 0.6

# Dropped before comparing. Function words plus the procedural vocabulary every
# other item on a council agenda uses — leaving them in makes every pair look
# alike, which is exactly the discrimination the match needs.
STOPWORDS = frozenset(
    """
    a an and the of or for to in on at by with from as is be
    motion resolution ordinance approving approval approve approved
    authorizing authorize accepting accept request requesting
    city ames council iowa staff report item agenda
    """.split()
)


class PacketLike(Protocol):
    """What :func:`match` needs off a packet item — see ``ItemSummary``."""

    title: str
    item_number: str


@dataclass
class Matching:
    """Which agenda entry each packet document belongs to."""

    # agenda index -> packet index. One-to-one in both directions.
    by_agenda: dict[int, int] = field(default_factory=dict)
    unmatched_agenda: list[int] = field(default_factory=list)
    unmatched_packet: list[int] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return len(self.by_agenda)


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _tokens(normalized: str) -> set[str]:
    return {
        token
        for token in normalized.split()
        if token not in STOPWORDS and (len(token) > 2 or token.isdigit())
    }


def _similarity(
    left_norm: str, left_tokens: set[str], right_norm: str, right_tokens: set[str]
) -> float:
    if not left_norm or not right_norm:
        return 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    if not left_tokens or not right_tokens:
        # Nothing distinctive survived the stopword filter — a title made
        # entirely of boilerplate. The character ratio is all there is.
        return sequence
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return TOKEN_WEIGHT * overlap + (1 - TOKEN_WEIGHT) * sequence


def match(
    agenda_items: Sequence[AgendaItem], packet_items: Sequence[PacketLike]
) -> Matching:
    """Join agenda entries to packet documents by title similarity.

    Greedy on the best-scoring pair first, which beats matching in agenda order:
    a confident pair claims its counterpart before a weaker one can steal it.
    The assignment is one-to-one, so an agenda item split across several PDFs
    keeps the closest and leaves the rest to render as unmatched packet items —
    visible and linked, just unplaced.
    """
    agenda_norm = [_normalize(a.title) for a in agenda_items]
    agenda_tokens = [_tokens(n) for n in agenda_norm]
    packet_norm = [_normalize(p.title) for p in packet_items]
    packet_tokens = [_tokens(n) for n in packet_norm]

    scored: list[tuple[float, int, int]] = []
    for a_idx, entry in enumerate(agenda_items):
        for p_idx, item in enumerate(packet_items):
            score = _similarity(
                agenda_norm[a_idx],
                agenda_tokens[a_idx],
                packet_norm[p_idx],
                packet_tokens[p_idx],
            )
            number = getattr(item, "item_number", "") or ""
            if entry.item_number and number.strip().lower() == entry.item_number.lower():
                # Deliberately uncapped: the score is a ranking key, not a
                # probability, and clamping at 1.0 would throw the bonus away
                # in the one case it decides anything — two identically titled
                # documents where only the number tells them apart.
                score += NUMBER_BONUS
            if score >= MATCH_THRESHOLD:
                scored.append((score, a_idx, p_idx))

    # Descending score; the index terms make ties resolve the same way every
    # run, which keeps a re-render of the same meeting byte-identical.
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))

    by_agenda: dict[int, int] = {}
    taken_packets: set[int] = set()
    for _, a_idx, p_idx in scored:
        if a_idx in by_agenda or p_idx in taken_packets:
            continue
        by_agenda[a_idx] = p_idx
        taken_packets.add(p_idx)

    return Matching(
        by_agenda=by_agenda,
        unmatched_agenda=[n for n in range(len(agenda_items)) if n not in by_agenda],
        unmatched_packet=[n for n in range(len(packet_items)) if n not in taken_packets],
    )
