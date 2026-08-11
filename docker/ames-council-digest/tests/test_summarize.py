"""Item records: coercing them, weighting them, and ordering them by the agenda."""

from __future__ import annotations

from datetime import datetime

import pytest

from ames_digest.agenda import AgendaOutline
from ames_digest.llm import Usage
from ames_digest.state import PHASE_OUTCOME, PHASE_PREVIEW
from ames_digest.summarize import (
    MAX_FACTS,
    SIGNIFICANCE_LEVELS,
    WEIGHT_LEVELS,
    ItemSummary,
    MeetingDigest,
    _coerce_facts,
    _coerce_summary,
    apply_outline,
    derive_weight,
)

from conftest import entry, item


class TestDeriveWeight:
    @pytest.mark.parametrize("significance, expected", [
        ("major", "major"),
        ("notable", "standard"),
        ("routine", "routine"),
    ])
    def test_off_the_consent_agenda(self, significance, expected):
        assert derive_weight(significance, consent=False) == expected

    @pytest.mark.parametrize("significance", ["major", "notable", "routine", ""])
    def test_consent_outranks_everything(self, significance):
        assert derive_weight(significance, consent=True) == "consent"

    @pytest.mark.parametrize("significance", ["", "spicy", "MAJOR", None])
    def test_unknown_significance_falls_back_to_routine(self, significance):
        assert derive_weight(significance, consent=False) == "routine"

    def test_every_combination_lands_in_the_vocabulary(self):
        assert all(
            derive_weight(s, consent=c) in WEIGHT_LEVELS
            for s in (*SIGNIFICANCE_LEVELS, "", "nonsense")
            for c in (True, False)
        )

    def test_weight_is_not_a_rename_of_significance(self):
        # Four structural values against three opinions. If these ever collapse
        # into each other, the docket has lost its consent distinction.
        assert len(WEIGHT_LEVELS) == 4 and len(SIGNIFICANCE_LEVELS) == 3
        assert set(WEIGHT_LEVELS) != set(SIGNIFICANCE_LEVELS)


class TestCoerceFacts:
    def test_not_a_list(self):
        assert _coerce_facts("nope") == []
        assert _coerce_facts(None) == []

    def test_well_formed(self):
        assert _coerce_facts([{"label": "Cost", "value": "$5"}]) == [
            {"label": "Cost", "value": "$5"}
        ]

    def test_half_a_fact_is_dropped(self):
        # A labelled blank or an unlabelled orphan both render as a broken cell.
        assert _coerce_facts([{"label": "Cost"}, {"value": "$5"}]) == []

    def test_non_dict_entries_dropped(self):
        assert _coerce_facts([None, "x", 3, {"label": "A", "value": "B"}]) == [
            {"label": "A", "value": "B"}
        ]

    def test_capped(self):
        many = [{"label": f"L{n}", "value": f"V{n}"} for n in range(20)]
        assert len(_coerce_facts(many)) == MAX_FACTS

    def test_long_values_clamped(self):
        facts = _coerce_facts([{"label": "L" * 200, "value": "V" * 200}])
        assert len(facts[0]["label"]) < 200 and len(facts[0]["value"]) < 200


class TestCoerceSummary:
    def test_empty_payload_yields_defaults(self):
        fields = _coerce_summary({})
        assert fields["summary"] == ""
        assert fields["significance"] == "routine"
        assert fields["amount"] is None
        assert fields["facts"] == []

    def test_all_nulls(self):
        keys = ["summary", "significance", "amount", "item_number", "item_type",
                "why_it_matters", "staff_recommendation", "facts", "source_page"]
        fields = _coerce_summary(dict.fromkeys(keys))
        assert fields["significance"] == "routine"
        assert fields["amount"] is None

    @pytest.mark.parametrize("raw, expected", [
        ("MAJOR", "major"), ("Notable", "notable"), ("routine", "routine"),
        ("critical", "routine"), ("", "routine"), (None, "routine"),
    ])
    def test_significance_normalized_then_validated(self, raw, expected):
        assert _coerce_summary({"significance": raw})["significance"] == expected

    def test_empty_amount_becomes_none(self):
        # Nothing downstream should render a dollar sign with no figure after it.
        assert _coerce_summary({"amount": ""})["amount"] is None
        assert _coerce_summary({"amount": "null"})["amount"] is None

    def test_real_amount_kept(self):
        assert _coerce_summary({"amount": "$1.2M"})["amount"] == "$1.2M"

    def test_keys_match_the_fields_the_model_owns(self):
        # `summarize_item` applies this wholesale via dataclasses.replace, so an
        # extra key here would be a TypeError at runtime rather than in a test.
        fields = _coerce_summary({})
        assert set(fields) <= {f for f in ItemSummary.__dataclass_fields__}

    def test_the_agenda_owns_section_and_weight(self):
        # These are derived from the agenda, never taken from the item model —
        # a model that volunteers them must not be able to set them.
        fields = _coerce_summary({"section": "CONSENT AGENDA", "weight": "major"})
        assert "section" not in fields and "weight" not in fields


class TestItemSummaryArchive:
    def test_full_round_trip(self):
        original = ItemSummary(
            code="A001", title="T", entry_id=7, url="https://x/7", page_count=3,
            last_modified=datetime(2026, 8, 7, 20, 25), summary="s",
            significance="notable", amount="$5", item_number="2", item_type="Motion",
            why_it_matters="w", staff_recommendation="r",
            facts=[{"label": "Cost", "value": "$5"}], source_page="4",
            section="CONSENT AGENDA", weight="consent",
        )
        assert ItemSummary.from_archive(original.to_archive()) == original

    def test_skipped_item_round_trips(self):
        # The appendix lists the whole packet, and that list has to survive the
        # update pass or items vanish once the minutes land.
        original = ItemSummary(code="A9", title="Scan", entry_id=9, url="u",
                               skipped="no extractable text (likely a scan)")
        assert ItemSummary.from_archive(original.to_archive()) == original

    def test_defaults_from_an_empty_payload(self):
        restored = ItemSummary.from_archive({})
        assert restored.entry_id == 0
        assert restored.weight == "routine"
        assert restored.skipped is None

    def test_unparseable_timestamp_degrades_to_none(self):
        assert ItemSummary.from_archive({"last_modified": "not a date"}).last_modified is None

    def test_missing_timestamp_is_none(self):
        assert ItemSummary.from_archive({"last_modified": None}).last_modified is None

    def test_facts_are_copied_not_shared(self):
        original = ItemSummary(code="", title="T", entry_id=1, url="u",
                               facts=[{"label": "A", "value": "B"}])
        archived = original.to_archive()
        archived["facts"][0]["label"] = "MUTATED"
        assert original.facts[0]["label"] == "A"

    def test_ok_requires_a_summary_and_no_skip(self):
        assert item("T", summary="s").ok
        assert not item("T", summary="").ok
        assert not item("T", summary="s", skipped="download failed").ok


class TestApplyOutline:
    def test_orders_items_by_the_agenda(self, outline):
        items = [
            item("Hearing on Annexation of Ames Golf and Country Club", eid=4),
            item("Motion approving payment of claims", eid=2),
            item("Motion approving Report of Change Orders", eid=3),
        ]
        _, ordered = apply_outline(outline, items)
        assert [i.entry_id for i in ordered] == [2, 3, 4]

    def test_stamps_agenda_number_and_section(self, outline):
        _, ordered = apply_outline(outline, [item("Motion approving payment of claims")])
        assert ordered[0].item_number == "2"
        assert ordered[0].section == "CONSENT AGENDA"

    def test_agenda_number_wins_over_the_documents_own(self, outline):
        _, ordered = apply_outline(
            outline, [item("Motion approving payment of claims", item_number="99")]
        )
        assert ordered[0].item_number == "2"

    def test_documents_number_used_when_the_agenda_has_none(self):
        outline = AgendaOutline(items=[entry("", "Payment of claims")])
        _, ordered = apply_outline(outline, [item("Payment of claims", item_number="7")])
        assert ordered[0].item_number == "7"

    def test_document_item_type_wins_over_the_agendas(self):
        # "Ordinance, second reading" beats a bare "Ordinance".
        outline = AgendaOutline(items=[entry("1", "Rezoning", item_type="Ordinance")])
        _, ordered = apply_outline(
            outline, [item("Rezoning", item_type="Ordinance, second reading")]
        )
        assert ordered[0].item_type == "Ordinance, second reading"

    def test_agenda_item_type_fills_in_when_the_document_is_silent(self):
        outline = AgendaOutline(items=[entry("1", "Rezoning", item_type="Ordinance")])
        _, ordered = apply_outline(outline, [item("Rezoning", item_type="")])
        assert ordered[0].item_type == "Ordinance"

    def test_consent_section_forces_consent_weight(self, outline):
        _, ordered = apply_outline(
            outline, [item("Motion approving payment of claims", "major")]
        )
        assert ordered[0].weight == "consent"

    def test_non_consent_section_takes_weight_from_significance(self, outline):
        _, ordered = apply_outline(
            outline, [item("Hearing on Annexation of Ames Golf and Country Club", "major")]
        )
        assert ordered[0].weight == "major"

    def test_unmatched_packet_item_lands_last_and_is_still_weighted(self, outline):
        items = [
            item("A document nobody put on the agenda", "notable", eid=9),
            item("Motion approving payment of claims", eid=2),
        ]
        _, ordered = apply_outline(outline, items)
        assert [i.entry_id for i in ordered] == [2, 9]
        assert ordered[-1].weight == "standard"
        assert ordered[-1].section == ""

    def test_unmatched_packet_items_keep_their_original_order(self, outline):
        items = [item("Zeta unrelated document", eid=1),
                 item("Alpha unrelated document", eid=2)]
        _, ordered = apply_outline(outline, items)
        assert [i.entry_id for i in ordered] == [1, 2]

    def test_unmatched_item_calling_itself_consent_gets_consent_weight(self, outline):
        _, ordered = apply_outline(
            outline, [item("Nothing like the agenda", "major", item_type="Consent")]
        )
        assert ordered[0].weight == "consent"

    def test_agenda_orphans_are_flagged_not_dropped(self, outline):
        flagged, _ = apply_outline(outline, [item("Motion approving payment of claims")])
        assert [o.item_number for o in flagged.orphans] == ["1", "3", "4"]

    def test_matched_entries_are_flagged(self, outline):
        flagged, _ = apply_outline(outline, [item("Motion approving payment of claims")])
        assert [e.matched for e in flagged.items] == [False, True, False, False]

    def test_nothing_is_ever_lost(self, outline):
        items = [item("Motion approving payment of claims"),
                 item("Unrelated one"), item("Unrelated two")]
        _, ordered = apply_outline(outline, items)
        assert len(ordered) == len(items)
        assert {i.entry_id for i in ordered} == {i.entry_id for i in items}

    def test_every_item_comes_back_with_a_known_weight(self, outline):
        items = [item("Motion approving payment of claims", "major"),
                 item("Unrelated", "notable"),
                 item("Scan", skipped="no text")]
        _, ordered = apply_outline(outline, items)
        assert all(i.weight in WEIGHT_LEVELS for i in ordered)

    def test_does_not_mutate_the_input_items(self, outline):
        original = item("Motion approving payment of claims")
        apply_outline(outline, [original])
        assert original.weight == "routine" and original.section == ""


class TestApplyOutlineWithoutAnAgenda:
    def test_items_pass_through_in_order(self):
        items = [item("A", eid=1), item("B", eid=2)]
        _, ordered = apply_outline(AgendaOutline(), items)
        assert [i.entry_id for i in ordered] == [1, 2]

    def test_weights_still_assigned(self):
        _, ordered = apply_outline(
            AgendaOutline(), [item("A", "major"), item("B", "notable")]
        )
        assert [i.weight for i in ordered] == ["major", "standard"]

    def test_self_declared_consent_still_recognized(self):
        # The document's own item_type is the only consent signal left.
        _, ordered = apply_outline(
            AgendaOutline(), [item("A", "major", item_type="Consent")]
        )
        assert ordered[0].weight == "consent"

    def test_outline_comes_back_empty(self):
        flagged, _ = apply_outline(AgendaOutline(), [item("A")])
        assert flagged.items == [] and flagged.orphans == []

    def test_time_and_place_survive_an_itemless_outline(self):
        outline = AgendaOutline(meeting_time="6:00 PM", location="City Hall")
        flagged, _ = apply_outline(outline, [item("A")])
        assert flagged.venue == "6:00 PM · City Hall"

    def test_no_items_at_all(self):
        flagged, ordered = apply_outline(AgendaOutline(), [])
        assert ordered == [] and flagged.items == []


class TestMeetingDigest:
    def test_total_usage_sums_both_passes(self):
        digest = MeetingDigest(
            meeting=None, body_markdown="", usage=Usage(10, 2, 1),
            prior_usage=Usage(300, 40, 41),
        )
        total = digest.total_usage
        assert (total.input_tokens, total.output_tokens, total.calls) == (310, 42, 42)

    def test_skipped_items(self):
        digest = MeetingDigest(
            meeting=None, body_markdown="",
            items=[item("ok", summary="s"), item("bad", skipped="no text")],
        )
        assert [i.title for i in digest.skipped_items] == ["bad"]

    def test_is_outcome(self):
        assert MeetingDigest(meeting=None, body_markdown="", kind=PHASE_OUTCOME).is_outcome
        assert not MeetingDigest(meeting=None, body_markdown="", kind=PHASE_PREVIEW).is_outcome

    def test_agenda_defaults_to_an_empty_outline(self):
        # Render reaches for digest.agenda.venue unconditionally.
        assert MeetingDigest(meeting=None, body_markdown="").agenda == AgendaOutline()
