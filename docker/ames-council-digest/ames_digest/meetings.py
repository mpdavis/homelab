"""Meeting discovery: pair each dated agenda folder with its council packet.

The clerk's repository keeps the agenda and the packet in parallel trees:

    Clerk Files/Agendas/<Board>/<Year>/<YYYY MMDD>/   -> the agenda PDF
    Clerk Files/Council Packet/<Year>/<YYYY MMDD>/    -> one PDF per agenda item

Only the City Council has a packet tree; other boards publish an agenda alone.
A meeting is keyed by date, and either side may be missing — packets are often
posted a few days after the agenda, and special meetings sometimes never get
one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from .weblink import Entry, WebLinkClient

log = logging.getLogger(__name__)

AGENDAS_FOLDER = "Agendas"
PACKET_FOLDER = "Council Packet"
# The packet tree is council-only; agendas for other boards live one level
# deeper under Agendas/<Board>.
COUNCIL_BOARD = "City Council"


@dataclass
class Meeting:
    """One dated meeting, with whatever documents are published for it."""

    board: str
    meeting_date: date
    # "" for a regular meeting. A labeled special session (e.g. "Tax Levy") can
    # fall on the same date as a regular one, so the label is part of a
    # meeting's identity, not decoration.
    label: str = ""
    agenda: Entry | None = None
    agenda_folder_id: int | None = None
    packet_master: Entry | None = None
    packet_items: list[Entry] = field(default_factory=list)
    packet_folder_id: int | None = None
    # False until MeetingSource.load_documents has listed this meeting's
    # folders, so an unexamined meeting is never mistaken for an empty one.
    documents_loaded: bool = False

    @property
    def key(self) -> str:
        """Stable identifier used for state tracking and output filenames."""
        parts = [self.board, self.meeting_date.isoformat(), self.label]
        slug = "-".join(p for p in parts if p).lower()
        return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")

    @property
    def display_name(self) -> str:
        return f"{self.board} — {self.label}" if self.label else self.board

    @property
    def has_documents(self) -> bool:
        return bool(self.agenda or self.packet_master or self.packet_items)

    def __str__(self) -> str:
        label = f" [{self.label}]" if self.label else ""
        return (
            f"{self.board}{label} {self.meeting_date.isoformat()} "
            f"(agenda={'yes' if self.agenda else 'no'}, items={len(self.packet_items)})"
        )


def _documents(client: WebLinkClient, folder_id: int) -> list[Entry]:
    return [
        e
        for e in client.list_folder(folder_id)
        if e.is_document and e.extension == "pdf"
    ]


def _split_master(entries: list[Entry]) -> tuple[Entry | None, list[Entry]]:
    """Separate the combined ``~Master`` PDF from the individual item PDFs."""
    master = next((e for e in entries if e.is_master), None)
    items = [e for e in entries if not e.is_master]
    # Items sort naturally by their agenda code (A001, A002, …); anything
    # without a code goes last so it doesn't interleave oddly.
    items.sort(key=lambda e: (e.item_code is None, e.item_code or e.name))
    return master, items


class MeetingSource:
    """Locates meetings for a board across one or more years."""

    def __init__(self, client: WebLinkClient, root_folder_id: int, board: str) -> None:
        self.client = client
        self.root_folder_id = root_folder_id
        self.board = board

    def _agenda_year_folders(self) -> dict[int, int]:
        parent = self.client.resolve_path(
            self.root_folder_id, AGENDAS_FOLDER, self.board
        )
        return self.client.year_folders(parent)

    def _packet_year_folders(self) -> dict[int, int]:
        if self.board != COUNCIL_BOARD:
            return {}
        try:
            parent = self.client.resolve_path(self.root_folder_id, PACKET_FOLDER)
        except LookupError:
            log.warning("no %r folder in the repository", PACKET_FOLDER)
            return {}
        return self.client.year_folders(parent)

    def discover(self, years: list[int]) -> list[Meeting]:
        """Return every meeting folder in the given years, oldest first.

        Only the folders — listing each meeting's documents costs one request
        per meeting per tree, which is the bulk of a run's traffic against the
        city's server. Callers narrow by date and digest state first, then call
        :meth:`load_documents` on what actually survives. A meeting comes back
        from here with no documents attached and ``has_documents`` False; that
        means "not looked at yet", not "empty".
        """
        agenda_years = self._agenda_year_folders()
        packet_years = self._packet_year_folders()

        # Keyed by (date, label) so a "Tax Levy" session and that day's regular
        # meeting stay separate. Packet folders carry no label, so an unlabeled
        # packet pairs with the unlabeled agenda — which is the regular meeting.
        meetings: dict[tuple[date, str], Meeting] = {}

        for year in years:
            folder_id = agenda_years.get(year)
            if folder_id is None:
                log.debug("no agenda folder for %s", year)
                continue
            for folder in self.client.meeting_folders(folder_id):
                meeting = meetings.setdefault(
                    (folder.meeting_date, folder.label),
                    Meeting(
                        board=self.board,
                        meeting_date=folder.meeting_date,
                        label=folder.label,
                    ),
                )
                meeting.agenda_folder_id = folder.entry_id

        for year in years:
            folder_id = packet_years.get(year)
            if folder_id is None:
                continue
            for folder in self.client.meeting_folders(folder_id):
                meeting = meetings.setdefault(
                    (folder.meeting_date, folder.label),
                    Meeting(
                        board=self.board,
                        meeting_date=folder.meeting_date,
                        label=folder.label,
                    ),
                )
                meeting.packet_folder_id = folder.entry_id

        return [m for _, m in sorted(meetings.items(), key=lambda kv: kv[0])]

    def load_documents(self, meeting: Meeting) -> Meeting:
        """Fetch the document listings for one meeting. Idempotent."""
        if meeting.documents_loaded:
            return meeting

        if meeting.agenda_folder_id is not None:
            # An agenda folder holds a single master PDF; if the clerk ever
            # posts several, prefer the master and keep the rest as items.
            master, extra = _split_master(
                _documents(self.client, meeting.agenda_folder_id)
            )
            meeting.agenda = master or (extra[0] if extra else None)

        if meeting.packet_folder_id is not None:
            master, items = _split_master(
                _documents(self.client, meeting.packet_folder_id)
            )
            meeting.packet_master = master
            meeting.packet_items = items

        meeting.documents_loaded = True
        return meeting
