"""Client for the City of Ames' public Laserfiche WebLink repository.

WebLink 11 serves its browse UI as an Angular app backed by ASP.NET page
methods. Two of them are all we need, and neither requires authentication on
the public COA repository:

    POST /WebLink/FolderListingService.aspx/GetFolderListing2  -> folder contents
    GET  /WebLink/0/edoc/<entryId>/<anything>.pdf              -> raw PDF bytes

The listing response carries a positional ``data`` array per entry whose
columns are described once by ``colTypes``; :func:`_zip_columns` turns that
back into a dict so callers can read page counts and the curated
"Document Description" field the clerk's office fills in per packet item.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator

import httpx

log = logging.getLogger(__name__)

# Laserfiche entry type discriminator on listing rows.
ENTRY_TYPE_FOLDER = 0
ENTRY_TYPE_DOCUMENT = -2

# Meeting folders are named "YYYY MMDD" (e.g. "2026 0728"), with two variants
# seen across the archive:
#   "2025 02040506"    a meeting spanning Feb 4, 5, and 6 — extra day pairs are
#                      appended, and the folder is keyed to the first day
#   "2026 0324 Tax Levy"  a labeled special meeting, which can share a date with
#                         that day's regular meeting
MEETING_FOLDER_RE = re.compile(r"^(\d{4})\s+(\d{2})(\d{2})((?:\d{2})*)\s*(.*)$")

# Packet items are prefixed with an agenda code: "A001 - Motion approving …".
ITEM_CODE_RE = re.compile(r"^([A-Z]{1,3}\d{2,3})\s*[-–]\s*(.*)$", re.DOTALL)

# The clerk's office prefixes the combined agenda/packet PDF with a tilde so it
# sorts first. It duplicates the individual items, so we treat it separately.
MASTER_PREFIX = "~Master"


@dataclass(frozen=True)
class Entry:
    """A folder or document in the repository."""

    entry_id: int
    name: str
    entry_type: int
    extension: str
    page_count: int | None = None
    description: str = ""

    @property
    def is_folder(self) -> bool:
        return self.entry_type == ENTRY_TYPE_FOLDER

    @property
    def is_document(self) -> bool:
        return self.entry_type == ENTRY_TYPE_DOCUMENT

    @property
    def is_master(self) -> bool:
        return self.name.startswith(MASTER_PREFIX)

    @property
    def item_code(self) -> str | None:
        """The agenda item code (``A001``) if this is a packet item."""
        m = ITEM_CODE_RE.match(self.name)
        return m.group(1) if m else None

    @property
    def title(self) -> str:
        """Item name with the agenda code prefix stripped."""
        m = ITEM_CODE_RE.match(self.name)
        return m.group(2).strip() if m else self.name


@dataclass(frozen=True)
class MeetingFolder:
    """A dated meeting folder such as ``Clerk Files/Agendas/City Council/2026/2026 0728``."""

    entry_id: int
    name: str
    meeting_date: date
    # "" for a regular meeting; "Tax Levy" and the like for special sessions.
    label: str = ""
    documents: list[Entry] = field(default_factory=list)


def parse_meeting_folder(folder_name: str) -> tuple[date, str] | None:
    """Parse a meeting folder name into its start date and optional label.

    Returns None if the name isn't a meeting folder at all.
    """
    m = MEETING_FOLDER_RE.match(folder_name.strip())
    if not m:
        return None
    try:
        start = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        # Folders occasionally carry a typo'd date (month 13, day 32, …).
        log.warning("meeting folder %r has an invalid date", folder_name)
        return None
    return start, m.group(5).strip()


def _zip_columns(col_types: list[dict], row: dict) -> dict:
    """Merge an entry's positional ``data`` array with the listing's column names."""
    values = row.get("data") or []
    return {
        col.get("name"): values[i]
        for i, col in enumerate(col_types)
        if i < len(values) and col.get("name")
    }


class WebLinkClient:
    """Read-only client for a public WebLink repository."""

    def __init__(
        self,
        base_url: str = "https://publicdocs.cityofames.org/WebLink",
        repo: str = "COA",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.repo = repo
        # Path resolution re-lists the same parent folders repeatedly; a run is
        # short enough that a plain per-run memo is both correct and kind to the
        # city's server.
        self._listing_cache: dict[int, list[Entry]] = {}
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "ames-council-digest/1.0 (+homelab)"},
            transport=httpx.HTTPTransport(retries=3),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WebLinkClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def list_folder(self, folder_id: int, page_size: int = 100) -> list[Entry]:
        """Return every entry in a folder, paging until the reported total is met."""
        cached = self._listing_cache.get(folder_id)
        if cached is not None:
            return cached

        entries: list[Entry] = []
        start = 0
        while True:
            payload = {
                "repoName": self.repo,
                "folderId": int(folder_id),
                "getNewListing": True,
                "start": start,
                "end": start + page_size,
                "sortColumn": "",
                "sortAscending": True,
            }
            resp = self._client.post(
                f"{self.base_url}/FolderListingService.aspx/GetFolderListing2",
                json=payload,
                headers={"X-Lf-Suppress-Login-Redirect": "1"},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            if data.get("failed"):
                raise RuntimeError(
                    f"WebLink listing failed for folder {folder_id}: {data.get('errMsg')}"
                )

            col_types = data.get("colTypes") or []
            rows = data.get("results") or []
            for row in rows:
                cols = _zip_columns(col_types, row)
                page_count = cols.get("PageCount")
                entries.append(
                    Entry(
                        entry_id=row["entryId"],
                        name=row.get("name", ""),
                        entry_type=row.get("type", 0),
                        extension=(row.get("extension") or "").lower(),
                        page_count=page_count if isinstance(page_count, int) else None,
                        description=(cols.get("f_Document Description") or "").strip(),
                    )
                )

            total = data.get("totalEntries")
            start += len(rows)
            if not rows or not isinstance(total, int) or start >= total:
                break

        self._listing_cache[folder_id] = entries
        return entries

    def resolve_path(self, root_id: int, *segments: str) -> int:
        """Walk folder names from ``root_id`` and return the final folder's id.

        Names are matched case-insensitively so a capitalization change upstream
        doesn't silently break discovery.
        """
        current = root_id
        for segment in segments:
            wanted = segment.strip().casefold()
            for entry in self.list_folder(current):
                if entry.is_folder and entry.name.strip().casefold() == wanted:
                    current = entry.entry_id
                    break
            else:
                raise LookupError(
                    f"folder {segment!r} not found under entry {current}"
                )
        return current

    def meeting_folders(self, parent_id: int) -> Iterator[MeetingFolder]:
        """Yield the ``YYYY MMDD`` meeting subfolders of a year folder, oldest first."""
        found: list[MeetingFolder] = []
        for entry in self.list_folder(parent_id):
            if not entry.is_folder:
                continue
            parsed = parse_meeting_folder(entry.name)
            if parsed is None:
                continue
            meeting_date, label = parsed
            found.append(
                MeetingFolder(
                    entry_id=entry.entry_id,
                    name=entry.name,
                    meeting_date=meeting_date,
                    label=label,
                )
            )
        yield from sorted(found, key=lambda f: (f.meeting_date, f.label))

    def year_folders(self, parent_id: int) -> dict[int, int]:
        """Map a four-digit year to the folder id holding that year's meetings."""
        years: dict[int, int] = {}
        for entry in self.list_folder(parent_id):
            if entry.is_folder and re.fullmatch(r"\d{4}", entry.name.strip()):
                years[int(entry.name.strip())] = entry.entry_id
        return years

    def document_url(self, entry_id: int) -> str:
        """Direct-download URL for a document's electronic file."""
        return f"{self.base_url}/0/edoc/{entry_id}/document.pdf"

    def viewer_url(self, entry_id: int) -> str:
        """Human-facing WebLink viewer URL, for linking from the digest."""
        return f"{self.base_url}/DocView.aspx?id={entry_id}&dbid=0&repo={self.repo}"

    def download(self, entry_id: int) -> bytes:
        """Fetch a document's raw bytes."""
        resp = self._client.get(self.document_url(entry_id))
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type.lower():
            raise RuntimeError(
                f"entry {entry_id} returned {content_type!r}, expected a PDF"
            )
        return resp.content
