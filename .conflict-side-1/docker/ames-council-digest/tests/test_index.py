"""The landing page, rebuilt by scanning the output directory rather than state."""

from __future__ import annotations

from datetime import date

import pytest

from ames_digest import index
from ames_digest.state import PHASE_OUTCOME, PHASE_PREVIEW, STATE_FILENAME, Totals

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="ames-digest-phase" content="{phase}">
<title>{title}</title>
</head>
<body>hello</body>
</html>
"""


def write_page(directory, stem, title="City Council — July 28, 2026",
               phase=PHASE_PREVIEW, markdown=True):
    (directory / f"{stem}.html").write_text(PAGE.format(title=title, phase=phase))
    if markdown:
        (directory / f"{stem}.md").write_text(f"# {title}\n")


class TestCompact:
    @pytest.mark.parametrize("value, expected", [
        (0, "0"), (999, "999"), (1_284, "1,284"), (9_999, "9,999"),
        (10_000, "10.0K"), (12_900, "12.9K"), (999_499, "999.5K"),
        (999_500, "1.00M"), (4_210_000, "4.21M"),
    ])
    def test(self, value, expected):
        assert index.compact(value) == expected

    def test_never_rounds_up_into_a_four_digit_k(self):
        # The K cutoff sits below 1,000,000 so nothing reads as "1000.0K".
        assert all("1000" not in index.compact(n)
                   for n in range(999_000, 1_000_500, 97))


class TestHeadParsing:
    def test_title_is_read_back_off_the_page(self):
        assert index._title_of(PAGE.format(title="A Title", phase=PHASE_PREVIEW), "x") == (
            "A Title"
        )

    def test_title_entities_are_unescaped(self):
        head = PAGE.format(title="Parks &amp; Recreation", phase=PHASE_PREVIEW)
        assert index._title_of(head, "x") == "Parks & Recreation"

    def test_missing_title_falls_back_to_the_stem(self):
        assert index._title_of("<html></html>", "the-stem") == "the-stem"

    def test_phase_is_read_back(self):
        assert index._phase_of(PAGE.format(title="t", phase=PHASE_OUTCOME)) == PHASE_OUTCOME

    def test_pages_without_a_phase_tag_are_previews(self):
        # Pages written before the tag existed report what they were.
        assert index._phase_of("<html></html>") == PHASE_PREVIEW

    def test_unknown_phase_value_falls_back(self):
        assert index._phase_of('<meta name="ames-digest-phase" content="banana">') == (
            PHASE_PREVIEW
        )

    @pytest.mark.parametrize("stem, expected", [
        ("city-council-2026-07-28", date(2026, 7, 28)),
        ("city-council-2026-03-24-tax-levy", date(2026, 3, 24)),
        ("city-council-2026-07-28-outcome", date(2026, 7, 28)),
        ("no-date-here", None),
        ("city-council-2026-13-45", None),
    ])
    def test_date_from_stem(self, stem, expected):
        assert index._date_of(stem) == expected


class TestDigestEntry:
    def _entry(self, stem, **kwargs):
        kwargs.setdefault("title", "T")
        kwargs.setdefault("meeting_date", date(2026, 7, 28))
        kwargs.setdefault("has_markdown", True)
        return index.DigestEntry(stem=stem, **kwargs)

    def test_undated_entries_sort_last_rather_than_crashing(self):
        undated = self._entry("x", meeting_date=None)
        dated = self._entry("y")
        assert sorted([dated, undated], key=lambda e: e.sort_key)[0] is undated


class TestCollect:
    def test_newest_meeting_first(self, tmp_path):
        write_page(tmp_path, "city-council-2026-07-28")
        write_page(tmp_path, "city-council-2026-08-11")
        assert [e.stem for e in index.collect(tmp_path)] == [
            "city-council-2026-08-11", "city-council-2026-07-28",
        ]

    def test_index_itself_is_skipped(self, tmp_path):
        write_page(tmp_path, "city-council-2026-07-28")
        (tmp_path / index.INDEX_FILENAME).write_text("<html></html>")
        assert len(index.collect(tmp_path)) == 1

    def test_markdown_presence_is_detected(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28", markdown=True)
        write_page(tmp_path, "b-2026-07-27", markdown=False)
        found = {e.stem: e.has_markdown for e in index.collect(tmp_path)}
        assert found["a-2026-07-28"] and not found["b-2026-07-27"]

    def test_empty_directory(self, tmp_path):
        assert index.collect(tmp_path) == []


class TestCollectMeetings:
    def test_one_row_per_meeting(self, tmp_path):
        write_page(tmp_path, "city-council-2026-07-28")
        write_page(tmp_path, "city-council-2026-08-11")
        assert len(index.collect_meetings(tmp_path)) == 2

    def test_each_page_is_its_own_meeting_row(self, tmp_path):
        write_page(tmp_path, "city-council-2026-07-28")
        write_page(tmp_path, "city-council-2026-07-28-outcome",
                   title="City Council — July 28, 2026 — what council decided")
        rows = index.collect_meetings(tmp_path)
        assert len(rows) == 2

    def test_row_title_prefers_the_page(self, tmp_path):
        write_page(tmp_path, "city-council-2026-07-28", title="The Real Title")
        assert index.collect_meetings(tmp_path)[0].title == "The Real Title"

    def test_page_title_is_read_without_legacy_rewriting(self, tmp_path):
        write_page(tmp_path, "city-council-2026-07-28-outcome",
                   title="City Council — July 28, 2026 — what council decided")
        assert index.collect_meetings(tmp_path)[0].title == (
            "City Council — July 28, 2026 — what council decided"
        )

    def test_updated_reflects_the_phase_tag(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28", phase=PHASE_OUTCOME)
        write_page(tmp_path, "b-2026-07-27", phase=PHASE_PREVIEW)
        rows = {r.meeting_stem: r.updated for r in index.collect_meetings(tmp_path)}
        assert rows["a-2026-07-28"] and not rows["b-2026-07-27"]


class TestRenderKpis:
    def test_omitted_when_there_is_nothing_to_report(self):
        assert index.render_kpis(None, None) == ""
        assert index.render_kpis(Totals(), None) == ""

    def test_tokens_and_digests(self):
        html = index.render_kpis(
            Totals(digests=3, input_tokens=294_000, output_tokens=33_000, calls=42), None
        )
        assert "Tokens used" in html and "Digests" in html and "Model calls" in html

    def test_call_tile_omitted_until_there_is_a_real_figure(self):
        html = index.render_kpis(
            Totals(digests=1, input_tokens=100, records_missing_calls=1), None
        )
        assert "Model calls" not in html

    def test_partial_call_data_is_marked_with_a_plus(self):
        html = index.render_kpis(
            Totals(digests=2, calls=40, records_missing_calls=1), None
        )
        assert "40+" in html

    def test_no_plus_when_every_record_has_calls(self):
        html = index.render_kpis(Totals(digests=2, calls=40), None)
        assert "40+" not in html

    def test_spend_tile_only_with_configured_prices(self):
        totals = Totals(digests=1, input_tokens=1_000_000, output_tokens=100_000)
        assert "Est. spend" not in index.render_kpis(totals, None)
        assert "$4.50" in index.render_kpis(totals, (3.0, 15.0))


class TestRenderIndex:
    def test_empty(self):
        html = index.render_index([])
        assert "No digests yet." in html

    def test_singular_and_plural_counts(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28")
        assert "1 meeting<" in index.render_index(index.collect_meetings(tmp_path))
        write_page(tmp_path, "b-2026-07-27")
        assert "2 meetings" in index.render_index(index.collect_meetings(tmp_path))

    def test_awaiting_the_minutes(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28", phase=PHASE_PREVIEW)
        assert "Awaiting the minutes" in index.render_index(
            index.collect_meetings(tmp_path)
        )

    def test_updated_after_the_meeting(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28", phase=PHASE_OUTCOME)
        html = index.render_index(index.collect_meetings(tmp_path))
        assert "Updated after the meeting" in html
        assert "Awaiting the minutes" not in html

    def test_preview_page_awaits_minutes_until_it_is_rewritten(self, tmp_path):
        write_page(tmp_path, "city-council-2026-07-28", phase=PHASE_PREVIEW)
        write_page(tmp_path, "city-council-2026-07-28-outcome", phase=PHASE_OUTCOME)
        html = index.render_index(index.collect_meetings(tmp_path))
        assert "Awaiting the minutes" in html

    def test_titles_are_escaped(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28", title="Parks &amp; Rec <script>")
        html = index.render_index(index.collect_meetings(tmp_path))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_undated_meeting_says_so(self, tmp_path):
        write_page(tmp_path, "no-date-page")
        assert "date unknown" in index.render_index(index.collect_meetings(tmp_path))


class TestRebuild:
    def test_writes_the_index_and_returns_the_count(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28")
        write_page(tmp_path, "b-2026-08-11")
        assert index.rebuild(tmp_path) == 2
        assert (tmp_path / index.INDEX_FILENAME).exists()

    def test_empty_directory_still_publishes_a_page(self, tmp_path):
        assert index.rebuild(tmp_path) == 0
        assert "No digests yet." in (tmp_path / index.INDEX_FILENAME).read_text()

    def test_totals_come_from_the_state_directory(self, tmp_path):
        output = tmp_path / "digests"
        state = tmp_path / "state"
        output.mkdir()
        state.mkdir()
        write_page(output, "a-2026-07-28")
        (state / STATE_FILENAME).write_text(
            '{"version": 2, "processed": {"a": {"preview": '
            '{"input_tokens": 1000, "output_tokens": 100, "calls": 5}}}}'
        )
        index.rebuild(output, state)
        assert "Tokens used" in (output / index.INDEX_FILENAME).read_text()

    def test_rebuilding_is_idempotent(self, tmp_path):
        write_page(tmp_path, "a-2026-07-28")
        index.rebuild(tmp_path)
        first = (tmp_path / index.INDEX_FILENAME).read_text()
        index.rebuild(tmp_path)
        assert (tmp_path / index.INDEX_FILENAME).read_text() == first
