"""Pairing a meeting's folders across the clerk's three document trees."""

from __future__ import annotations

from datetime import date

import pytest

from ames_digest.meetings import Meeting, _split_master
from ames_digest.weblink import ENTRY_TYPE_DOCUMENT, Entry


def doc(name: str, entry_id: int = 1) -> Entry:
    return Entry(entry_id, name, ENTRY_TYPE_DOCUMENT, "pdf")


class TestKey:
    def test_regular_meeting(self):
        meeting = Meeting(board="City Council", meeting_date=date(2026, 7, 28))
        assert meeting.key == "city-council-2026-07-28"

    def test_labeled_session_is_a_distinct_key(self):
        # A special session can share a date with that day's regular meeting, so
        # the label is part of identity rather than decoration.
        regular = Meeting(board="City Council", meeting_date=date(2026, 3, 24))
        levy = Meeting(board="City Council", meeting_date=date(2026, 3, 24),
                       label="Tax Levy")
        assert levy.key == "city-council-2026-03-24-tax-levy"
        assert regular.key != levy.key

    def test_punctuation_is_flattened(self):
        meeting = Meeting(board="Parks & Recreation", meeting_date=date(2026, 1, 5))
        assert meeting.key == "parks-recreation-2026-01-05"

    def test_key_is_filesystem_safe(self):
        # The archive refuses anything that could climb out of its directory.
        from ames_digest.archive import SAFE_KEY_RE
        meeting = Meeting(board="Ames/Story County Board", meeting_date=date(2026, 1, 5),
                          label="Joint · Session")
        assert SAFE_KEY_RE.match(meeting.key)


class TestDisplayName:
    def test_without_a_label(self):
        assert Meeting("City Council", date(2026, 7, 28)).display_name == "City Council"

    def test_with_a_label(self):
        meeting = Meeting("City Council", date(2026, 3, 24), label="Tax Levy")
        assert meeting.display_name == "City Council — Tax Levy"


class TestDocumentPredicates:
    def test_nothing_published(self):
        meeting = Meeting("City Council", date(2026, 7, 28))
        assert not meeting.has_documents
        assert not meeting.has_preview_documents
        assert not meeting.has_minutes

    def test_agenda_alone_is_preview_material(self):
        meeting = Meeting("City Council", date(2026, 7, 28), agenda=doc("~Master"))
        assert meeting.has_documents and meeting.has_preview_documents

    def test_packet_items_alone_are_preview_material(self):
        meeting = Meeting("City Council", date(2026, 7, 28),
                          packet_items=[doc("A001 - x")])
        assert meeting.has_preview_documents

    def test_minutes_alone_are_not_preview_material(self):
        meeting = Meeting("City Council", date(2026, 7, 28), minutes=doc("~Master"))
        assert meeting.has_documents
        assert not meeting.has_preview_documents
        assert meeting.has_minutes

    def test_documents_loaded_defaults_false(self):
        # "Not looked at yet" must never read as "empty".
        assert not Meeting("City Council", date(2026, 7, 28)).documents_loaded


class TestSplitMaster:
    def test_separates_the_combined_pdf(self):
        entries = [doc("A002 - Two", 2), doc("~Master - Everything", 9), doc("A001 - One", 1)]
        master, items = _split_master(entries)
        assert master.entry_id == 9
        assert [i.entry_id for i in items] == [1, 2]

    def test_items_sort_by_agenda_code(self):
        entries = [doc("A010 - Ten"), doc("A002 - Two"), doc("A001 - One")]
        _, items = _split_master(entries)
        assert [i.item_code for i in items] == ["A001", "A002", "A010"]

    def test_codeless_items_sort_last(self):
        entries = [doc("Loose attachment"), doc("A001 - One")]
        _, items = _split_master(entries)
        assert [i.name for i in items] == ["A001 - One", "Loose attachment"]

    def test_no_master(self):
        master, items = _split_master([doc("A001 - One")])
        assert master is None and len(items) == 1

    def test_empty(self):
        assert _split_master([]) == (None, [])

    def test_only_a_master(self):
        master, items = _split_master([doc("~Master - Everything")])
        assert master is not None and items == []

    @pytest.mark.parametrize("codes", [
        ["A001", "A002", "A003"], ["A003", "A001", "A002"], ["A002", "A003", "A001"],
    ])
    def test_sort_is_order_independent(self, codes):
        _, items = _split_master([doc(f"{c} - x") for c in codes])
        assert [i.item_code for i in items] == ["A001", "A002", "A003"]
