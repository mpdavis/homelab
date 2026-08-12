"""Splicing outcomes into a page that was written before the meeting.

This is line-oriented string surgery on Markdown the model wrote, so the cases
that matter are the ones where it did not write what the prompt asked for.
"""

from __future__ import annotations

import pytest

from ames_digest import merge

PAGE = """\
## Notable Topics

- Council takes up the water rate.

## Additional Reading

- **Water rate increase** — Staff recommends a 6% increase.
- **Fire station siting** — A new station on the north side.

## Public input

None scheduled.
"""


class TestNormalize:
    def test_folds_case_and_punctuation(self):
        assert merge.normalize("Water Rate: Increase!") == "water rate increase"

    def test_collapses_runs(self):
        assert merge.normalize("a---b___c") == "a b c"

    def test_strips_edges(self):
        assert merge.normalize("  **Water** ") == "water"

    def test_two_spellings_of_one_label_agree(self):
        assert merge.normalize("Water-rate increase") == merge.normalize(
            "Water rate increase"
        )


class TestLabels:
    def test_reads_bolded_labels_in_order(self):
        assert merge.labels(PAGE) == ["Water rate increase", "Fire station siting"]

    def test_no_section_yields_nothing(self):
        assert merge.labels("## Notable Topics\n\n- Just this.\n") == []

    def test_empty_page(self):
        assert merge.labels("") == []

    def test_paragraph_style_bullets_are_still_found(self):
        # Left to itself the model writes these as bare paragraphs with no list
        # marker. Requiring a "-" silently found nothing to attach outcomes to.
        page = ("## Additional Reading\n\n"
                "**Water rate increase** — Staff recommends 6%.\n\n"
                "**Fire station siting** — A new station.\n")
        assert merge.labels(page) == ["Water rate increase", "Fire station siting"]

    def test_numbered_lists_are_bullets_too(self):
        page = "## Additional Reading\n\n1. **Water rate** — Up 6%.\n2. **Fire** — New.\n"
        assert merge.labels(page) == ["Water rate", "Fire"]

    def test_unlabelled_bullet_falls_back_to_its_opening_words(self):
        page = "## Additional Reading\n\n- Staff recommends a 6% increase to rates.\n"
        assert merge.labels(page) == ["Staff recommends a 6% increase to rates."]

    def test_long_label_is_clamped(self):
        page = f"## Additional Reading\n\n- **{'x' * 200}** — text.\n"
        assert len(merge.labels(page)[0]) <= merge.MAX_LABEL_CHARS

    def test_section_ends_at_the_next_heading_of_the_same_level(self):
        assert "None scheduled." not in merge.labels(PAGE)

    def test_deeper_headings_do_not_end_the_section(self):
        page = ("## Additional Reading\n\n- **A** — one.\n\n"
                "### A sub-heading\n\n- **B** — two.\n\n## Public input\n\nNone.\n")
        assert merge.labels(page) == ["A", "B"]

    def test_heading_match_is_case_and_punctuation_insensitive(self):
        assert merge.labels("## ADDITIONAL READING!\n\n- **A** — one.\n") == ["A"]


class TestApplyUpdates:
    def test_attaches_outcomes_by_label(self):
        matched, total, body = merge.apply_updates(
            PAGE, {"Water rate increase": "Approved 5-1.",
                   "Fire station siting": "Referred to staff."}
        )
        assert (matched, total) == (2, 2)
        assert "Approved 5-1." in body and "Referred to staff." in body

    def test_wraps_updates_in_the_red_span(self):
        _, _, body = merge.apply_updates(PAGE, {"Water rate increase": "Approved."})
        assert f'<span style="color:{merge.UPDATE_COLOR}">' in body
        assert merge.UPDATE_PREFIX in body

    def test_unmatched_bullet_gets_the_not_recorded_default(self):
        matched, total, body = merge.apply_updates(
            PAGE, {"Water rate increase": "Approved 5-1."}
        )
        assert (matched, total) == (1, 2)
        assert merge.NO_UPDATE in body

    def test_every_bullet_gets_exactly_one_update(self):
        _, _, body = merge.apply_updates(PAGE, {})
        assert body.count(merge.UPDATE_PREFIX) == 2

    def test_matching_ignores_case_and_punctuation(self):
        matched, _, _ = merge.apply_updates(PAGE, {"water-rate increase": "Approved."})
        assert matched == 1

    def test_label_the_model_invented_is_dropped(self):
        matched, total, body = merge.apply_updates(
            PAGE, {"Some other item entirely": "Approved 5-1."}
        )
        assert (matched, total) == (0, 2)
        assert "Approved 5-1." not in body, "an invented label must not land anywhere"

    def test_blank_outcome_counts_as_no_outcome(self):
        matched, _, _ = merge.apply_updates(PAGE, {"Water rate increase": "   "})
        assert matched == 0

    def test_no_section_is_reported_not_crashed(self):
        matched, total, body = merge.apply_updates("## Notable Topics\n\n- x\n", {"A": "B"})
        assert (matched, total) == (0, 0)
        assert body == "## Notable Topics\n\n- x\n"

    def test_rerunning_replaces_rather_than_stacks(self):
        _, _, once = merge.apply_updates(PAGE, {"Water rate increase": "Approved."})
        _, _, twice = merge.apply_updates(once, {"Water rate increase": "Approved."})
        assert once == twice
        assert twice.count(merge.UPDATE_PREFIX) == 2

    def test_rerunning_can_change_an_outcome(self):
        _, _, once = merge.apply_updates(PAGE, {"Water rate increase": "Approved."})
        _, _, twice = merge.apply_updates(once, {"Water rate increase": "Rescinded."})
        assert "Rescinded." in twice and "Approved." not in twice

    def test_update_lands_on_the_last_line_of_a_multiline_bullet(self):
        page = ("## Additional Reading\n\n"
                "- **Water rate** — Staff recommends\n"
                "  a 6% increase to rates.\n\n"
                "## Public input\n\nNone.\n")
        _, _, body = merge.apply_updates(page, {"Water rate": "Approved."})
        lines = body.splitlines()
        assert lines[3].endswith("</span>"), lines
        assert "a 6% increase to rates." in lines[3]

    def test_loose_lists_do_not_push_updates_onto_a_blank_line(self):
        page = ("## Additional Reading\n\n"
                "- **A** — one.\n\n"
                "- **B** — two.\n\n"
                "## Public input\n\nNone.\n")
        _, _, body = merge.apply_updates(page, {"A": "Approved.", "B": "Denied."})
        for line in body.splitlines():
            if merge.UPDATE_PREFIX in line:
                assert line.strip().startswith("-"), line

    def test_content_outside_the_section_is_untouched(self):
        _, _, body = merge.apply_updates(PAGE, {"Water rate increase": "Approved."})
        assert "- Council takes up the water rate." in body
        assert body.endswith("None scheduled.\n") or "None scheduled." in body

    def test_the_page_keeps_its_bullets(self):
        _, _, body = merge.apply_updates(PAGE, {})
        assert "**Water rate increase**" in body and "**Fire station siting**" in body


class TestPromoteSentinels:
    def test_turns_a_sentinel_tail_into_a_span(self):
        body = f"- **Water rate** — Up 6%. {merge.SENTINEL} Approved 5-1."
        result = merge.promote_sentinels(body)
        assert merge.SENTINEL not in result
        assert f'<span style="color:{merge.UPDATE_COLOR}">' in result
        assert "Approved 5-1." in result

    def test_handles_several_bullets(self):
        body = (f"- **A** — one. {merge.SENTINEL} Approved.\n"
                f"- **B** — two. {merge.SENTINEL} Denied.")
        result = merge.promote_sentinels(body)
        assert result.count(merge.UPDATE_PREFIX) == 2

    def test_page_without_sentinels_is_unchanged(self):
        assert merge.promote_sentinels("- **A** — one.") == "- **A** — one."


class TestAppendSection:
    def test_appends(self):
        result = merge.append_section("# Page\n\nBody.", "After the meeting", "- A thing.")
        assert result.endswith("## After the meeting\n\n- A thing.\n")

    @pytest.mark.parametrize("content", ["", "   ", "\n\n"])
    def test_empty_content_leaves_the_page_alone(self, content):
        body = "# Page\n\nBody."
        assert merge.append_section(body, "After the meeting", content) == body

    def test_does_not_pile_up_blank_lines(self):
        result = merge.append_section("# Page\n\n\n\n", "T", "x")
        assert "\n\n\n" not in result


class TestPlaintext:
    def test_unwraps_spans_keeping_the_words(self):
        _, _, body = merge.apply_updates(PAGE, {"Water rate increase": "Approved 5-1."})
        text = merge.plaintext(body)
        assert "<span" not in text and "</span>" not in text
        assert "Approved 5-1." in text and merge.UPDATE_PREFIX in text

    def test_plain_body_is_unchanged(self):
        assert merge.plaintext("- **A** — one.") == "- **A** — one."
