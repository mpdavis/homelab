"""Shared builders for the test suite.

Nothing here touches the network or a model gateway. Every test in this suite
runs against constructed inputs, which is what makes it worth running on every
PR: the pipeline's expensive halves are stubbed at their seams, and what is
actually exercised is the logic that decides ordering, weight, matching,
merging, and what reaches the page.
"""

from __future__ import annotations

import pytest

from ames_digest.agenda import AgendaItem, AgendaOutline
from ames_digest.summarize import ItemSummary


def make_pdf(pages: list[str]) -> bytes:
    """A minimal, valid PDF carrying real extractable text, one stream per page.

    Written by hand rather than with a library: pypdf can assemble pages but
    not lay down text, and the whole point is to give ``pdftext.extract`` a
    document with a genuine text layer to find.
    """
    objects: dict[int, str] = {}
    count = len(pages)
    font_id = 2 * count + 3
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))

    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Count {count} /Kids [{kids}] >>"
    for i, text in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        )
        shown = "\n".join(f"({line}) Tj 0 -14 Td" for line in text.split("\n"))
        stream = f"BT /F1 12 Tf 40 750 Td\n{shown}\nET"
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    objects[font_id] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objects[num]}\nendobj\n".encode("latin-1")

    xref_at = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode("latin-1")
    for num in range(1, size):
        out += f"{offsets.get(num, 0):010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def item(title: str, significance: str = "routine", **kwargs) -> ItemSummary:
    """A summarized packet item, with only the fields a test cares about set.

    ``eid`` is a shorthand for ``entry_id``, which tests reach for often enough
    to be worth the alias — it is the only handle on two otherwise identical
    items.
    """
    if "eid" in kwargs:
        kwargs["entry_id"] = kwargs.pop("eid")
    kwargs.setdefault("entry_id", abs(hash(title)) % 100_000 or 1)
    kwargs.setdefault("url", f"https://example.test/{kwargs['entry_id']}")
    kwargs.setdefault("summary", "What council is asked to do.")
    return ItemSummary(
        code=kwargs.pop("code", ""),
        title=title,
        significance=significance,
        **kwargs,
    )


def entry(number: str, title: str, section: str = "", **kwargs) -> AgendaItem:
    return AgendaItem(item_number=number, title=title, section=section, **kwargs)


@pytest.fixture
def outline() -> AgendaOutline:
    """A small agenda: a presentation, two consent items, one hearing."""
    return AgendaOutline(
        items=[
            entry("1", "Child Care Feasibility Study", "PRESENTATION"),
            entry("2", "Motion approving payment of claims", "CONSENT AGENDA"),
            entry("3", "Motion approving Report of Change Orders", "CONSENT AGENDA"),
            entry("4", "Hearing on Annexation of Ames Golf and Country Club", "HEARINGS"),
        ],
        meeting_time="6:00 PM",
        location="City Hall, 515 Clark Ave",
    )
