"""Agenda segmentation: coercing the model's outline, and joining it to the packet."""

from __future__ import annotations

import pytest

from ames_digest.agenda import (
    MATCH_THRESHOLD,
    MAX_AGENDA_ITEMS,
    AgendaItem,
    AgendaOutline,
    coerce_outline,
    match,
)

from conftest import entry, item


class TestCoerceOutline:
    def test_empty_payload(self):
        assert coerce_outline({}) == AgendaOutline()

    def test_items_not_a_list(self):
        assert coerce_outline({"items": "nope"}).items == []

    def test_all_nulls(self):
        assert coerce_outline(
            {"items": None, "meeting_time": None, "location": None}
        ) == AgendaOutline()

    def test_non_dict_entries_dropped(self):
        assert coerce_outline({"items": [None, 3, "x", {"title": "Real"}]}).items == [
            AgendaItem(title="Real")
        ]

    def test_titleless_entry_dropped(self):
        # A numbered blank renders as a hole on the page.
        assert coerce_outline({"items": [{"item_number": "9"}]}).items == []

    def test_fields_coerced(self):
        out = coerce_outline({"items": [{
            "item_number": 14,
            "title": "  Rezone   the\n thing ",
            "item_type": {"not": "a string"},
            "section": "CONSENT AGENDA",
        }]})
        assert out.items[0] == AgendaItem(
            item_number="14", title="Rezone the thing", item_type="",
            section="CONSENT AGENDA",
        )

    def test_time_and_location(self):
        out = coerce_outline({"meeting_time": "6:00 PM", "location": "City Hall"})
        assert (out.meeting_time, out.location) == ("6:00 PM", "City Hall")

    def test_item_count_capped(self):
        payload = {"items": [{"title": f"item {n}"} for n in range(MAX_AGENDA_ITEMS + 50)]}
        assert len(coerce_outline(payload).items) == MAX_AGENDA_ITEMS

    def test_order_preserved(self):
        out = coerce_outline({"items": [{"title": "b"}, {"title": "a"}, {"title": "c"}]})
        assert [i.title for i in out.items] == ["b", "a", "c"]

    def test_matched_defaults_false(self):
        # Matching is the only thing allowed to set this.
        assert coerce_outline({"items": [{"title": "x"}]}).items[0].matched is False


class TestConsent:
    @pytest.mark.parametrize("section", [
        "CONSENT AGENDA", "Consent Agenda", "consent", "ITEMS ON CONSENT",
    ])
    def test_recognized(self, section):
        assert AgendaItem(title="x", section=section).is_consent

    @pytest.mark.parametrize("section", ["HEARINGS", "", "ORDINANCES", "CONSENTING"])
    def test_not_recognized(self, section):
        # "CONSENTING" must not match: the check is word-bounded, not substring.
        assert not AgendaItem(title="x", section=section).is_consent


class TestOutlineHelpers:
    def test_venue_joins_both_halves(self):
        assert AgendaOutline(meeting_time="6:00 PM", location="City Hall").venue == (
            "6:00 PM · City Hall"
        )

    @pytest.mark.parametrize("kwargs, expected", [
        ({"meeting_time": "6:00 PM"}, "6:00 PM"),
        ({"location": "City Hall"}, "City Hall"),
        ({}, ""),
    ])
    def test_venue_with_a_missing_half(self, kwargs, expected):
        assert AgendaOutline(**kwargs).venue == expected

    def test_orphans_are_the_unmatched_entries(self, outline):
        flagged = outline.with_matches({0, 2})
        assert [o.item_number for o in flagged.orphans] == ["2", "4"]

    def test_with_matches_does_not_mutate_the_original(self, outline):
        outline.with_matches({0, 1, 2, 3})
        assert all(not i.matched for i in outline.items)

    def test_with_matches_keeps_time_and_place(self, outline):
        assert outline.with_matches(set()).venue == outline.venue


class TestOutlineArchive:
    def test_round_trip(self, outline):
        flagged = outline.with_matches({1, 3})
        assert AgendaOutline.from_archive(flagged.to_archive()) == flagged

    def test_round_trip_preserves_orphans(self, outline):
        flagged = outline.with_matches({1})
        restored = AgendaOutline.from_archive(flagged.to_archive())
        assert [o.item_number for o in restored.orphans] == ["1", "3", "4"]

    @pytest.mark.parametrize("payload", [None, "", [], 42])
    def test_non_dict_payload_yields_an_empty_outline(self, payload):
        assert AgendaOutline.from_archive(payload) == AgendaOutline()

    def test_missing_items_key(self):
        assert AgendaOutline.from_archive({"meeting_time": "6:00 PM"}) == AgendaOutline(
            meeting_time="6:00 PM"
        )

    def test_junk_entries_skipped(self):
        restored = AgendaOutline.from_archive({"items": [None, 1, {"title": "Kept"}]})
        assert [i.title for i in restored.items] == ["Kept"]


class TestMatch:
    def test_exact_titles(self):
        agenda = [entry("2", "Motion approving payment of claims")]
        packet = [item("Motion approving payment of claims")]
        assert match(agenda, packet).by_agenda == {0: 0}

    def test_agenda_wording_differs_from_the_filename(self):
        # The real failure mode: the clerk's filename and the agenda line
        # describe one item in different words.
        agenda = [entry("31", "Hearing on 2025/26 Low Point Drainage Improvements "
                              "Program project (Various Locations)")]
        packet = [item("Hearing on 2025/26 Low Point Drainage Improvements Program "
                       "project (Various Locations)")]
        assert match(agenda, packet).by_agenda == {0: 0}

    def test_unrelated_titles_do_not_match(self):
        result = match(
            [entry("1", "Rezoning of 4899 Everest Avenue")],
            [item("Motion approving payment of claims")],
        )
        assert result.by_agenda == {}
        assert result.unmatched_agenda == [0]
        assert result.unmatched_packet == [0]

    def test_shared_boilerplate_alone_is_not_a_match(self):
        # Both are "Resolution approving ..." — if procedural vocabulary counted,
        # these would pair up.
        result = match(
            [entry("1", "Resolution approving the purchase of playground equipment")],
            [item("Resolution approving the annexation of farmland on 260th Street")],
        )
        assert result.by_agenda == {}

    def test_best_pair_wins_over_agenda_order(self):
        agenda = [
            entry("1", "Request from City of Slater to withdraw from Resource Recovery"),
            entry("2", "Request from City of Zearing to withdraw from Resource Recovery"),
        ]
        packet = [
            item("Request from City of Zearing to withdraw from Resource Recovery"),
            item("Request from City of Slater to withdraw from Resource Recovery"),
        ]
        assert match(agenda, packet).by_agenda == {0: 1, 1: 0}

    def test_one_agenda_entry_claims_only_one_of_several_documents(self):
        result = match(
            [entry("23", "Fitch Family Indoor Aquatic Center")],
            [item("Fitch Family Indoor Aquatic Center", eid=1),
             item("Fitch Family Indoor Aquatic Center", eid=2)],
        )
        assert len(result.by_agenda) == 1
        assert len(result.unmatched_packet) == 1

    def test_two_agenda_entries_cannot_claim_the_same_document(self):
        # The other direction, and the one that actually happens: a real agenda
        # carries three near-identical withdrawal requests. If the clerk posts
        # only one of the PDFs, the second entry has to go orphan rather than
        # pointing at the first entry's document and duplicating it on the page.
        agenda = [
            entry("12", "Request from City of Kelley to withdraw from Resource "
                        "Recovery System 28E Intergovernmental Agreement"),
            entry("13", "Request from City of Slater to withdraw from Resource "
                        "Recovery System 28E Intergovernmental Agreement"),
        ]
        packet = [item("Request from City of Kelley to withdraw from Resource "
                       "Recovery System 28E Intergovernmental Agreement")]
        result = match(agenda, packet)
        assert result.by_agenda == {0: 0}
        assert result.unmatched_agenda == [1]
        assert result.unmatched_packet == []

    def test_no_document_is_assigned_twice_across_a_whole_meeting(self):
        # The invariant behind both cases above, stated once over a realistic
        # mix: whatever the scores say, each PDF is spoken for at most once.
        agenda = [entry(str(n), f"Change Order No. {n} for the aquatic center")
                  for n in range(1, 6)]
        packet = [item(f"Change Order No. {n} for the aquatic center", eid=n)
                  for n in (2, 4)]
        result = match(agenda, packet)
        claimed = list(result.by_agenda.values())
        assert len(claimed) == len(set(claimed))
        assert len(claimed) <= len(packet)

    def test_printed_item_number_breaks_a_tie(self):
        # Two uploads of the same agreement; only the number tells them apart.
        result = match(
            [entry("18", "Water monitoring services agreement")],
            [item("Water monitoring services agreement", eid=1, item_number="19"),
             item("Water monitoring services agreement", eid=2, item_number="18")],
        )
        assert result.by_agenda == {0: 1}

    def test_number_agreement_cannot_rescue_an_unrelated_title(self):
        result = match(
            [entry("5", "Rezoning of 4899 Everest Avenue")],
            [item("Motion approving payment of claims", item_number="5")],
        )
        assert result.by_agenda == {}

    def test_empty_agenda(self):
        result = match([], [item("x"), item("y")])
        assert result.by_agenda == {}
        assert result.unmatched_packet == [0, 1]

    def test_empty_packet(self):
        result = match([entry("1", "x")], [])
        assert result.unmatched_agenda == [0]

    def test_both_empty(self):
        result = match([], [])
        assert (result.by_agenda, result.unmatched_agenda, result.unmatched_packet) == (
            {}, [], []
        )

    def test_orphan_indices_are_sorted(self):
        agenda = [entry("1", "Payment of claims"), entry("2", "Nothing alike at all")]
        packet = [item("Totally unrelated document"),
                  item("Payment of claims"),
                  item("Another unrelated one")]
        result = match(agenda, packet)
        assert result.unmatched_agenda == [1]
        assert result.unmatched_packet == [0, 2]

    def test_matched_count(self, outline):
        packet = [item("Motion approving payment of claims"),
                  item("Motion approving Report of Change Orders")]
        assert match(outline.items, packet).matched == 2

    def test_is_deterministic(self):
        # A re-render of the same meeting must be byte-identical, which means
        # ties have to resolve the same way every run.
        agenda = [entry("1", "Change Order No. 1"), entry("2", "Change Order No. 2")]
        packet = [item("Change Order No. 3"), item("Change Order No. 4")]
        first = match(agenda, packet).by_agenda
        assert all(match(agenda, packet).by_agenda == first for _ in range(5))

    def test_titles_that_are_pure_boilerplate_fall_back_to_characters(self):
        # Nothing survives the stopword filter on either side, so the character
        # ratio is all there is — and it still has to work.
        result = match([entry("1", "Motion approving")], [item("Motion approving")])
        assert result.by_agenda == {0: 0}

    def test_empty_titles_never_match(self):
        assert match([entry("1", "")], [item("")]).by_agenda == {}

    def test_threshold_is_a_sane_middle(self):
        assert 0.2 < MATCH_THRESHOLD < 0.8
