"""Reading the city's Laserfiche listings: names, columns, and paging."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ames_digest.weblink import (
    ENTRY_TYPE_DOCUMENT,
    ENTRY_TYPE_FOLDER,
    Entry,
    WebLinkClient,
    _parse_listing_datetime,
    _zip_columns,
    parse_meeting_folder,
)


class TestParseMeetingFolder:
    def test_regular_meeting(self):
        assert parse_meeting_folder("2026 0728") == (date(2026, 7, 28), "")

    def test_multi_day_meeting_keys_to_the_first_day(self):
        # "2025 02040506" is February 4, 5 and 6.
        assert parse_meeting_folder("2025 02040506") == (date(2025, 2, 4), "")

    def test_labeled_special_session(self):
        assert parse_meeting_folder("2026 0324 Tax Levy") == (date(2026, 3, 24), "Tax Levy")

    def test_multi_day_with_a_label(self):
        assert parse_meeting_folder("2025 02040506 Budget") == (
            date(2025, 2, 4), "Budget"
        )

    def test_surrounding_whitespace(self):
        assert parse_meeting_folder("  2026 0728  ") == (date(2026, 7, 28), "")

    @pytest.mark.parametrize("name", [
        "2026 1332",   # month 13
        "2026 0230",   # February 30
        "2026 0000",
    ])
    def test_impossible_dates_are_rejected(self, name):
        assert parse_meeting_folder(name) is None

    @pytest.mark.parametrize("name", [
        "Not a meeting", "", "2026", "Agendas", "26 0728", "2026-07-28",
    ])
    def test_non_meeting_folders(self, name):
        assert parse_meeting_folder(name) is None


class TestParseListingDatetime:
    def test_full_timestamp(self):
        assert _parse_listing_datetime("8/7/2026 8:25:02 PM") == datetime(
            2026, 8, 7, 20, 25, 2
        )

    def test_without_seconds(self):
        assert _parse_listing_datetime("8/7/2026 8:25 PM") == datetime(2026, 8, 7, 20, 25)

    def test_date_only(self):
        assert _parse_listing_datetime("8/7/2026") == datetime(2026, 8, 7)

    def test_non_breaking_space_before_the_meridiem(self):
        assert _parse_listing_datetime("8/7/2026 8:25:02\xa0PM") == datetime(
            2026, 8, 7, 20, 25, 2
        )

    def test_am_is_not_pm(self):
        assert _parse_listing_datetime("8/7/2026 8:25:02 AM").hour == 8

    @pytest.mark.parametrize("raw", [None, "", "   ", 42, "not a date", "2026-08-07"])
    def test_unparseable_yields_none(self, raw):
        assert _parse_listing_datetime(raw) is None


class TestZipColumns:
    def test_pairs_names_with_positions(self):
        cols = [{"name": "PageCount"}, {"name": "LastModified"}]
        assert _zip_columns(cols, {"data": [7, "8/7/2026"]}) == {
            "PageCount": 7, "LastModified": "8/7/2026"
        }

    def test_short_data_row_is_tolerated(self):
        cols = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        assert _zip_columns(cols, {"data": [1]}) == {"A": 1}

    def test_unnamed_columns_are_skipped(self):
        cols = [{"name": ""}, {"name": "B"}]
        assert _zip_columns(cols, {"data": [1, 2]}) == {"B": 2}

    def test_missing_data_key(self):
        assert _zip_columns([{"name": "A"}], {}) == {}


class TestEntry:
    def test_item_code_and_title(self):
        e = Entry(1, "A001 - Motion approving payment of claims", ENTRY_TYPE_DOCUMENT, "pdf")
        assert e.item_code == "A001"
        assert e.title == "Motion approving payment of claims"

    def test_en_dash_separator(self):
        assert Entry(1, "A001 – Something", ENTRY_TYPE_DOCUMENT, "pdf").item_code == "A001"

    def test_multiline_title_survives(self):
        e = Entry(1, "A001 - Line one\nline two", ENTRY_TYPE_DOCUMENT, "pdf")
        assert e.title == "Line one\nline two"

    def test_name_without_a_code(self):
        e = Entry(1, "Just a document", ENTRY_TYPE_DOCUMENT, "pdf")
        assert e.item_code is None
        assert e.title == "Just a document"

    def test_master_is_not_an_item(self):
        e = Entry(1, "~Master - July 28, 2026 Agenda", ENTRY_TYPE_DOCUMENT, "pdf")
        assert e.is_master
        assert e.item_code is None, "the master must not be mistaken for item A-something"

    def test_ordinary_document_is_not_master(self):
        assert not Entry(1, "A001 - Thing", ENTRY_TYPE_DOCUMENT, "pdf").is_master

    def test_type_discriminators(self):
        folder = Entry(1, "2026", ENTRY_TYPE_FOLDER, "")
        document = Entry(2, "x.pdf", ENTRY_TYPE_DOCUMENT, "pdf")
        assert folder.is_folder and not folder.is_document
        assert document.is_document and not document.is_folder

    def test_defaults(self):
        e = Entry(1, "x", ENTRY_TYPE_DOCUMENT, "pdf")
        assert e.page_count is None and e.description == "" and e.last_modified is None


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.requests = []

    def post(self, url, json=None, headers=None):
        self.requests.append(json)
        return FakeResponse(self.pages.pop(0))

    def close(self):
        return None


def listing(rows, total, col_types=None):
    return {"data": {
        "colTypes": col_types or [{"name": "PageCount"}, {"name": "LastModified"},
                                  {"name": "f_Document Description"}],
        "results": rows,
        "totalEntries": total,
    }}


def row(entry_id, name, page_count=1, modified="8/7/2026 8:25:02 PM", description=""):
    return {"entryId": entry_id, "name": name, "type": ENTRY_TYPE_DOCUMENT,
            "extension": "PDF", "data": [page_count, modified, description]}


class TestListFolder:
    def test_maps_columns_onto_entries(self):
        http = FakeHTTP(listing([row(7, "A001 - Thing", 12, description="  Clerk note ")], 1))
        entries = WebLinkClient(client=http).list_folder(100)
        assert len(entries) == 1
        assert entries[0].entry_id == 7
        assert entries[0].page_count == 12
        assert entries[0].description == "Clerk note", "description is stripped"
        assert entries[0].last_modified == datetime(2026, 8, 7, 20, 25, 2)
        assert entries[0].extension == "pdf", "extension is lowercased"

    def test_pages_until_the_reported_total(self):
        http = FakeHTTP(
            listing([row(n, f"A00{n} - x") for n in range(1, 4)], 5),
            listing([row(n, f"A00{n} - x") for n in range(4, 6)], 5),
        )
        assert len(WebLinkClient(client=http).list_folder(100, page_size=3)) == 5
        assert len(http.requests) == 2
        assert http.requests[1]["start"] == 3

    def test_stops_on_an_empty_page(self):
        http = FakeHTTP(listing([row(1, "x")], 99), listing([], 99))
        assert len(WebLinkClient(client=http).list_folder(100)) == 1

    def test_caches_per_folder(self):
        http = FakeHTTP(listing([row(1, "x")], 1))
        client = WebLinkClient(client=http)
        client.list_folder(100)
        client.list_folder(100)
        assert len(http.requests) == 1, "a run re-lists the same parents constantly"

    def test_non_integer_page_count_becomes_none(self):
        http = FakeHTTP(listing([row(1, "x", page_count="n/a")], 1))
        assert WebLinkClient(client=http).list_folder(100)[0].page_count is None

    def test_a_failed_listing_raises(self):
        http = FakeHTTP({"data": {"failed": True, "errMsg": "no such folder"}})
        with pytest.raises(RuntimeError, match="no such folder"):
            WebLinkClient(client=http).list_folder(100)

    def test_missing_total_stops_after_one_page(self):
        http = FakeHTTP(listing([row(1, "x")], None))
        assert len(WebLinkClient(client=http).list_folder(100)) == 1


class TestFolderNavigation:
    def _client(self, *pages):
        return WebLinkClient(client=FakeHTTP(*pages))

    def test_year_folders(self):
        rows = [
            {"entryId": 1, "name": "2025", "type": ENTRY_TYPE_FOLDER, "data": []},
            {"entryId": 2, "name": " 2026 ", "type": ENTRY_TYPE_FOLDER, "data": []},
            {"entryId": 3, "name": "Archive", "type": ENTRY_TYPE_FOLDER, "data": []},
            {"entryId": 4, "name": "2027", "type": ENTRY_TYPE_DOCUMENT, "data": []},
        ]
        assert self._client(listing(rows, 4)).year_folders(1) == {2025: 1, 2026: 2}

    def test_meeting_folders_are_sorted_oldest_first(self):
        rows = [
            {"entryId": 1, "name": "2026 0728", "type": ENTRY_TYPE_FOLDER, "data": []},
            {"entryId": 2, "name": "2026 0324 Tax Levy", "type": ENTRY_TYPE_FOLDER,
             "data": []},
            {"entryId": 3, "name": "Notes", "type": ENTRY_TYPE_FOLDER, "data": []},
        ]
        folders = list(self._client(listing(rows, 3)).meeting_folders(1))
        assert [(f.meeting_date, f.label) for f in folders] == [
            (date(2026, 3, 24), "Tax Levy"), (date(2026, 7, 28), ""),
        ]

    def test_resolve_path_is_case_insensitive(self):
        rows = [{"entryId": 42, "name": "Clerk FILES", "type": ENTRY_TYPE_FOLDER,
                 "data": []}]
        assert self._client(listing(rows, 1)).resolve_path(1, "clerk files") == 42

    def test_resolve_path_missing_segment(self):
        with pytest.raises(LookupError, match="not found"):
            self._client(listing([], 0)).resolve_path(1, "Nope")

    def test_resolve_path_ignores_documents_with_the_same_name(self):
        rows = [{"entryId": 9, "name": "Agendas", "type": ENTRY_TYPE_DOCUMENT, "data": []}]
        with pytest.raises(LookupError):
            self._client(listing(rows, 1)).resolve_path(1, "Agendas")


class TestUrls:
    def test_document_and_viewer_urls(self):
        client = WebLinkClient(base_url="https://docs.test/WebLink/", repo="COA",
                               client=FakeHTTP())
        assert client.document_url(7) == "https://docs.test/WebLink/0/edoc/7/document.pdf"
        assert client.viewer_url(7) == (
            "https://docs.test/WebLink/DocView.aspx?id=7&dbid=0&repo=COA"
        )
