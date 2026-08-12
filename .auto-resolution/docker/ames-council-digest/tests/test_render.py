"""What actually reaches the page: header, body, appendix, footer."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ames_digest.agenda import AgendaOutline
from ames_digest.llm import Usage
from ames_digest.meetings import Meeting
from ames_digest.render import filename_stem, render, subject_line
from ames_digest.state import PHASE_OUTCOME, PHASE_PREVIEW
from ames_digest.summarize import MeetingDigest

from conftest import entry, item

BODY = "## Notable Topics\n\n- Council takes up the water rate.\n"


def digest(**kwargs) -> MeetingDigest:
    kwargs.setdefault("meeting", Meeting("City Council", date(2026, 7, 28)))
    kwargs.setdefault("body_markdown", BODY)
    kwargs.setdefault("model", "claude-sonnet-4-5")
    kwargs.setdefault("generated_at", datetime(2026, 7, 26, 9, 30))
    return MeetingDigest(**kwargs)


class TestTitleAndFilename:
    def test_subject_line(self):
        assert subject_line(digest()) == "City Council — July 28, 2026"

    def test_labeled_session(self):
        meeting = Meeting("City Council", date(2026, 3, 24), label="Tax Levy")
        assert subject_line(digest(meeting=meeting)) == (
            "City Council — Tax Levy — March 24, 2026"
        )

    def test_both_passes_write_the_same_file(self):
        # One meeting, one URL, whose content grows once the minutes land.
        preview = digest(kind=PHASE_PREVIEW)
        outcome = digest(kind=PHASE_OUTCOME)
        assert filename_stem(preview) == filename_stem(outcome) == "city-council-2026-07-28"

    def test_title_does_not_change_between_passes(self):
        assert subject_line(digest(kind=PHASE_PREVIEW)) == subject_line(
            digest(kind=PHASE_OUTCOME)
        )


class TestSubtitle:
    def test_venue_then_source_links(self):
        out = render(digest(
            agenda=AgendaOutline(meeting_time="6:00 PM", location="City Hall, 515 Clark Ave"),
            agenda_url="https://x/agenda", packet_url="https://x/packet",
        ))
        assert "6:00 PM · City Hall, 515 Clark Ave" in out.markdown
        assert out.markdown.index("6:00 PM") < out.markdown.index("[Agenda]")

    def test_venue_reaches_the_html(self):
        out = render(digest(agenda=AgendaOutline(meeting_time="6:00 PM")))
        assert "6:00 PM" in out.html

    def test_no_venue_configured(self):
        out = render(digest(agenda_url="https://x/agenda"))
        assert out.markdown.splitlines()[2].startswith("[Agenda]")

    def test_minutes_link_leads_when_present(self):
        out = render(digest(minutes_url="https://x/min", agenda_url="https://x/agenda"))
        assert out.markdown.index("[Minutes]") < out.markdown.index("[Agenda]")

    def test_absent_links_are_omitted(self):
        out = render(digest())
        assert "[Minutes]" not in out.markdown and "[Agenda]" not in out.markdown


class TestAppendix:
    def test_lists_every_packet_item_with_a_link(self):
        out = render(digest(items=[
            item("Payment of claims", code="A001", url="https://x/1"),
            item("Change orders", code="A002", url="https://x/2"),
        ]))
        assert "### Every item in this packet" in out.markdown
        assert "**A001** [Payment of claims](https://x/1)" in out.markdown
        assert "**A002** [Change orders](https://x/2)" in out.markdown

    def test_significance_annotated_for_non_routine_items(self):
        out = render(digest(items=[item("Big thing", "major", code="A001")]))
        assert "_major_" in out.markdown

    def test_amount_rides_along_with_significance(self):
        out = render(digest(items=[
            item("Big thing", "major", code="A001", amount="$2.3M")
        ]))
        assert "_major, $2.3M_" in out.markdown

    def test_routine_items_are_not_annotated(self):
        out = render(digest(items=[item("Small thing", "routine", code="A001")]))
        assert "_routine_" not in out.markdown

    def test_skipped_items_report_their_reason(self):
        out = render(digest(items=[
            item("A scan", code="A001", summary="", skipped="no extractable text")
        ]))
        assert "not summarized: no extractable text" in out.markdown

    def test_long_skip_reasons_are_truncated(self):
        # Skip reasons can carry a whole HTTP error body.
        out = render(digest(items=[
            item("A scan", code="A001", summary="", skipped="x" * 500)
        ]))
        assert "…" in out.markdown
        assert "x" * 200 not in out.markdown

    def test_skip_reason_whitespace_is_collapsed(self):
        out = render(digest(items=[
            item("A scan", code="A001", summary="", skipped="line one\n  line two")
        ]))
        assert "line one line two" in out.markdown

    def test_item_without_a_code(self):
        out = render(digest(items=[item("No code", url="https://x/1")]))
        assert "- [No code](https://x/1)" in out.markdown


class TestAgendaOrphans:
    def _digest(self):
        outline = AgendaOutline(items=[
            entry("1", "Child Care Feasibility Study", "PRESENTATION"),
            entry("2", "Payment of claims", "CONSENT AGENDA", matched=True),
        ])
        return digest(items=[item("Payment of claims", code="A001")], agenda=outline)

    def test_orphans_get_their_own_appendix_section(self):
        out = render(self._digest())
        assert "### On the agenda, with no packet document" in out.markdown
        assert "**1** Child Care Feasibility Study" in out.markdown

    def test_orphan_section_notes_the_agenda_section(self):
        assert "_PRESENTATION_" in render(self._digest()).markdown

    def test_matched_entries_are_not_listed_as_orphans(self):
        markdown = render(self._digest()).markdown
        tail = markdown.split("### On the agenda, with no packet document")[1]
        assert "Payment of claims" not in tail

    def test_orphans_reach_the_html(self):
        assert "Child Care Feasibility Study" in render(self._digest()).html

    def test_orphans_render_even_with_no_packet_items_at_all(self):
        outline = AgendaOutline(items=[entry("1", "Child Care Feasibility Study")])
        out = render(digest(agenda=outline))
        assert "Child Care Feasibility Study" in out.markdown
        assert "### Every item in this packet" not in out.markdown

    def test_no_appendix_when_there_is_nothing_to_list(self):
        out = render(digest())
        assert "### Every item" not in out.markdown
        assert "no packet document" not in out.markdown


class TestFooter:
    def test_reports_both_passes_worth_of_usage(self):
        # Billing only the update pass would show a few thousand tokens for a
        # page whose packet summaries cost half a million.
        out = render(digest(kind=PHASE_OUTCOME, usage=Usage(2_000, 300, 1),
                            prior_usage=Usage(294_000, 33_000, 41)))
        assert "42 model calls" in out.markdown
        assert "296,000 in / 33,300 out tokens" in out.markdown

    def test_names_the_model(self):
        assert "claude-sonnet-4-5" in render(digest()).markdown

    def test_counts_packet_items(self):
        out = render(digest(items=[item("a"), item("b")]))
        assert "2 packet items" in out.markdown

    def test_mentions_the_minutes_on_an_outcome(self):
        out = render(digest(kind=PHASE_OUTCOME, minutes_url="https://x/m"))
        assert "official summary minutes" in out.markdown

    def test_no_sources_at_all(self):
        assert "no source documents" in render(digest()).markdown

    def test_outcome_reports_both_timestamps(self):
        out = render(digest(kind=PHASE_OUTCOME,
                            preview_generated_at=datetime(2026, 7, 26, 9, 30),
                            generated_at=datetime(2026, 8, 4, 11, 0)))
        assert "Generated 2026-07-26 09:30, updated 2026-08-04 11:00" in out.markdown

    def test_carries_the_machine_generated_caveat(self):
        assert "machine-generated" in render(digest()).markdown

    def test_a_revised_page_says_so(self):
        out = render(digest(revision=2))
        assert "rebuilt after 2 source revisions" in out.markdown

    def test_one_revision_reads_singular(self):
        markdown = render(digest(revision=1)).markdown
        assert "rebuilt after 1 source revision" in markdown
        assert "revisions" not in markdown

    def test_an_unrevised_page_says_nothing_about_revisions(self):
        assert "rebuilt" not in render(digest()).markdown


class TestHtmlAndText:
    def test_phase_meta_tag_records_which_pass_wrote_the_page(self):
        # The index reads this back, which keeps the page self-describing.
        assert '<meta name="ames-digest-phase" content="outcome">' in render(
            digest(kind=PHASE_OUTCOME)
        ).html

    def test_title_tag_matches_the_subject(self):
        out = render(digest())
        assert f"<title>{out.subject}</title>" in out.html

    def test_body_markdown_is_converted(self):
        assert "<h2" in render(digest()).html

    def test_inline_styles_are_applied(self):
        # Email clients strip <style> blocks, so the spacing is spelled out.
        assert "<h2 style=" in render(digest()).html

    def test_text_form_strips_the_update_markup(self):
        body = ('## Additional Reading\n\n- **Water rate** — Up 6%. '
                '<span style="color:#b42318">**Update:** Approved 5-1.</span>\n')
        out = render(digest(kind=PHASE_OUTCOME, body_markdown=body))
        assert "<span" not in out.text
        assert "Approved 5-1." in out.text

    def test_markdown_leads_with_the_title(self):
        assert render(digest()).markdown.startswith("# City Council — July 28, 2026")

    def test_rendered_fields_are_populated(self):
        out = render(digest())
        assert out.subject and out.markdown and out.html and out.text
        assert out.filename_stem == "city-council-2026-07-28"
