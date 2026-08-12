"""Run state: which meetings have been digested, in which pass, at what cost.

Dropping state costs real money — every lost entry is a packet re-summarized —
so the migration and salvage paths are the point of this file.
"""

from __future__ import annotations

import json

import pytest

from ames_digest.state import (
    PHASE_OUTCOME,
    PHASE_PREVIEW,
    STATE_FILENAME,
    STATE_VERSION,
    State,
    Totals,
)

KEY = "city-council-2026-07-28"


def write_state(tmp_path, payload):
    (tmp_path / STATE_FILENAME).write_text(json.dumps(payload))
    return State.load(tmp_path)


class TestLoad:
    def test_missing_file_starts_empty(self, tmp_path):
        assert State.load(tmp_path).processed == {}

    def test_corrupt_file_starts_empty_but_is_left_on_disk(self, tmp_path):
        path = tmp_path / STATE_FILENAME
        path.write_text("{not json")
        assert State.load(tmp_path).processed == {}
        assert path.exists(), "a corrupt ledger is kept for inspection"

    def test_current_version_loads_as_is(self, tmp_path):
        state = write_state(tmp_path, {
            "version": STATE_VERSION,
            "processed": {KEY: {PHASE_PREVIEW: {"calls": 3}}},
        })
        assert state.seen(KEY, PHASE_PREVIEW)

    def test_v1_records_migrate_to_preview(self, tmp_path):
        # v1 predates the outcome pass, so every record was a preview. Re-keying
        # them is what stops an upgrade re-billing every packet on disk.
        state = write_state(tmp_path, {
            "version": 1,
            "processed": {KEY: {"digested_at": "2026-07-28", "input_tokens": 100}},
        })
        assert state.seen(KEY, PHASE_PREVIEW)
        assert not state.seen(KEY, PHASE_OUTCOME)
        assert state.processed[KEY][PHASE_PREVIEW]["input_tokens"] == 100

    def test_v1_non_dict_records_dropped(self, tmp_path):
        state = write_state(tmp_path, {"version": 1, "processed": {KEY: "nonsense"}})
        assert state.processed == {}

    def test_unknown_version_salvages_phase_shaped_entries(self, tmp_path):
        state = write_state(tmp_path, {
            "version": 99,
            "processed": {
                KEY: {PHASE_PREVIEW: {"calls": 1}},
                "junk": {"something-else": {}},
            },
        })
        assert state.seen(KEY, PHASE_PREVIEW)
        assert "junk" not in state.processed

    def test_missing_processed_key(self, tmp_path):
        assert write_state(tmp_path, {"version": STATE_VERSION}).processed == {}


class TestSeenAndRecord:
    def test_unknown_meeting(self, tmp_path):
        assert not State.load(tmp_path).seen(KEY)

    def test_record_then_seen(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, items=40)
        assert state.seen(KEY, PHASE_PREVIEW)

    def test_phases_are_tracked_separately(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW)
        assert not state.seen(KEY, PHASE_OUTCOME)
        state.record(KEY, PHASE_OUTCOME)
        assert state.seen(KEY, PHASE_PREVIEW) and state.seen(KEY, PHASE_OUTCOME)

    def test_record_stamps_a_time_and_keeps_details(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, items=40, model="m")
        entry = state.processed[KEY][PHASE_PREVIEW]
        assert entry["items"] == 40 and entry["model"] == "m"
        assert entry["digested_at"]

    def test_recording_again_replaces_that_phase_only(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, items=1)
        state.record(KEY, PHASE_OUTCOME, items=2)
        state.record(KEY, PHASE_PREVIEW, items=3)
        assert state.processed[KEY][PHASE_PREVIEW]["items"] == 3
        assert state.processed[KEY][PHASE_OUTCOME]["items"] == 2


class TestSave:
    def test_round_trip(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, items=40)
        state.save()
        assert State.load(tmp_path).seen(KEY, PHASE_PREVIEW)

    def test_writes_the_current_version(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY)
        state.save()
        payload = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert payload["version"] == STATE_VERSION

    def test_creates_the_directory(self, tmp_path):
        target = tmp_path / "deep" / "nested"
        state = State.load(target)
        state.record(KEY)
        state.save()
        assert (target / STATE_FILENAME).exists()

    def test_leaves_no_temporary_files(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY)
        state.save()
        assert [p.name for p in tmp_path.iterdir()] == [STATE_FILENAME]


class TestTotals:
    def test_empty(self, tmp_path):
        totals = State.load(tmp_path).totals()
        assert (totals.digests, totals.meetings, totals.input_tokens) == (0, 0, 0)

    def test_sums_across_phases_and_meetings(self, tmp_path):
        state = write_state(tmp_path, {"version": STATE_VERSION, "processed": {
            KEY: {
                PHASE_PREVIEW: {"input_tokens": 100, "output_tokens": 10, "calls": 41},
                PHASE_OUTCOME: {"input_tokens": 20, "output_tokens": 5, "calls": 1},
            },
            "city-council-2026-08-11": {
                PHASE_PREVIEW: {"input_tokens": 200, "output_tokens": 30, "calls": 40},
            },
        }})
        totals = state.totals()
        assert totals.meetings == 2
        assert totals.digests == 3
        assert (totals.previews, totals.outcomes) == (2, 1)
        assert (totals.input_tokens, totals.output_tokens) == (320, 45)
        assert totals.calls == 82
        assert totals.total_tokens == 365

    def test_records_predating_call_tracking_are_counted_separately(self, tmp_path):
        # They contribute tokens but no call count, so the total would otherwise
        # read as an undercount with no explanation.
        state = write_state(tmp_path, {"version": STATE_VERSION, "processed": {
            KEY: {PHASE_PREVIEW: {"input_tokens": 100}},
            "other": {PHASE_PREVIEW: {"input_tokens": 50, "calls": 4}},
        }})
        totals = state.totals()
        assert totals.records_missing_calls == 1
        assert totals.calls == 4

    def test_an_explicit_zero_call_count_is_not_missing(self, tmp_path):
        state = write_state(tmp_path, {"version": STATE_VERSION, "processed": {
            KEY: {PHASE_PREVIEW: {"calls": 0}},
        }})
        assert state.totals().records_missing_calls == 0

    def test_malformed_entries_are_skipped(self, tmp_path):
        state = write_state(tmp_path, {"version": STATE_VERSION, "processed": {
            KEY: {PHASE_PREVIEW: {"calls": 1}, "junk": "not a dict"},
        }})
        assert state.totals().digests == 1

    def test_missing_token_fields_default_to_zero(self, tmp_path):
        state = write_state(tmp_path, {"version": STATE_VERSION, "processed": {
            KEY: {PHASE_PREVIEW: {}},
        }})
        assert state.totals().input_tokens == 0


class TestCost:
    def test_at_configured_rates(self):
        totals = Totals(input_tokens=1_000_000, output_tokens=100_000)
        assert totals.cost(3.0, 15.0) == pytest.approx(3.0 + 1.5)

    def test_zero_usage(self):
        assert Totals().cost(3.0, 15.0) == 0.0


class TestFreshnessFields:
    """v3: the fingerprint and manifest a pass was recorded with."""

    def test_v2_records_survive_untouched(self, tmp_path):
        # v3 is purely additive. The absence of `folders` is itself the signal
        # that this record predates fingerprinting.
        state = write_state(tmp_path, {
            "version": 2,
            "processed": {KEY: {PHASE_PREVIEW: {"input_tokens": 100}}},
        })
        assert state.seen(KEY, PHASE_PREVIEW)
        assert state.entry(KEY, PHASE_PREVIEW)["input_tokens"] == 100
        assert "folders" not in state.entry(KEY, PHASE_PREVIEW)

    def test_v1_migrates_all_the_way_forward(self, tmp_path):
        state = write_state(tmp_path, {
            "version": 1, "processed": {KEY: {"input_tokens": 100}},
        })
        assert state.seen(KEY, PHASE_PREVIEW)

    def test_current_version_written(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, folders={"packet": "x"})
        state.save()
        assert json.loads((tmp_path / STATE_FILENAME).read_text())["version"] == 3

    def test_entry_returns_the_record(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, folders={"packet": "x"})
        assert state.entry(KEY, PHASE_PREVIEW)["folders"] == {"packet": "x"}

    def test_entry_is_none_for_a_pass_that_never_ran(self, tmp_path):
        assert State.load(tmp_path).entry(KEY, PHASE_OUTCOME) is None
        assert State.load(tmp_path).entry("nonexistent") is None

    def test_entry_ignores_a_malformed_record(self, tmp_path):
        state = write_state(tmp_path, {
            "version": 3, "processed": {KEY: {PHASE_PREVIEW: "not a dict"}},
        })
        assert state.entry(KEY, PHASE_PREVIEW) is None

    def test_fingerprint_and_manifest_round_trip(self, tmp_path):
        state = State.load(tmp_path)
        state.record(
            KEY, PHASE_PREVIEW,
            folders={"packet": "8/11/2026 2:16:54 PM"},
            documents={"7": {"mod": "8/11/2026 2:12:00 PM", "pages": 3}},
        )
        state.save()
        record = State.load(tmp_path).entry(KEY, PHASE_PREVIEW)
        assert record["folders"]["packet"] == "8/11/2026 2:16:54 PM"
        assert record["documents"]["7"]["pages"] == 3


class TestRefresh:
    def test_re_baselines_without_claiming_a_digest_happened(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, folders={"packet": "old"}, documents={})
        digested_at = state.entry(KEY, PHASE_PREVIEW)["digested_at"]

        assert state.refresh(
            KEY, PHASE_PREVIEW,
            folders={"packet": "new"}, documents={"1": {"mod": "m"}},
        )
        record = state.entry(KEY, PHASE_PREVIEW)
        assert record["folders"] == {"packet": "new"}
        assert record["documents"] == {"1": {"mod": "m"}}
        assert record["digested_at"] == digested_at
        assert record["rechecked_at"]

    def test_refreshing_a_pass_that_never_ran_does_nothing(self, tmp_path):
        state = State.load(tmp_path)
        assert not state.refresh(KEY, PHASE_PREVIEW, folders={}, documents={})
        assert state.entry(KEY, PHASE_PREVIEW) is None

    def test_survives_a_save_and_reload(self, tmp_path):
        state = State.load(tmp_path)
        state.record(KEY, PHASE_PREVIEW, folders={"packet": "old"}, documents={})
        state.refresh(KEY, PHASE_PREVIEW, folders={"packet": "new"}, documents={})
        state.save()
        assert State.load(tmp_path).entry(KEY, PHASE_PREVIEW)["folders"] == {
            "packet": "new"
        }
