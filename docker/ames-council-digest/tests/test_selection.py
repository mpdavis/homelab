"""Which passes a run decides to spend money on.

Every job selected here is a packet download and ~40 model calls, so the rules
about what is skipped matter as much as the rules about what runs.
"""

from __future__ import annotations

import argparse
from datetime import date

import pytest

from ames_digest.__main__ import _pending_phases, _select, _years_for_window
from ames_digest.meetings import Meeting
from ames_digest.state import PHASE_OUTCOME, PHASE_PREVIEW, State
from ames_digest.weblink import ENTRY_TYPE_DOCUMENT, Entry

TODAY = date(2026, 7, 30)


def doc(name="~Master", entry_id=1):
    return Entry(entry_id, name, ENTRY_TYPE_DOCUMENT, "pdf")


def args(**overrides):
    base = {"phase": "both", "force": False, "meeting": None, "limit": 3,
            "lookahead_days": 10}
    base.update(overrides)
    return argparse.Namespace(**base)


def meeting(day=28, month=7, **kwargs):
    kwargs.setdefault("board", "City Council")
    return Meeting(meeting_date=date(2026, month, day), **kwargs)


def full_packet(day=28, month=7):
    return meeting(day, month, agenda=doc(), packet_items=[doc("A001 - x", 2)])


class FakeSource:
    """Stands in for MeetingSource; records which meetings cost a listing."""

    def __init__(self):
        self.loaded = []

    def load_documents(self, m):
        self.loaded.append(m.key)
        m.documents_loaded = True
        return m


class TestYearsForWindow:
    def test_single_year(self):
        assert _years_for_window(date(2026, 1, 1), date(2026, 12, 31)) == [2026]

    def test_spans_the_new_year(self):
        # A late-December run still has to see January's meetings.
        assert _years_for_window(date(2026, 12, 20), date(2027, 1, 5)) == [2026, 2027]


class TestPendingPhases:
    def test_past_meeting_with_everything_published(self):
        m = meeting(agenda=doc(), packet_items=[doc("A001 - x", 2)], minutes=doc("~M", 3))
        assert _pending_phases(m, args(), None, TODAY) == [PHASE_PREVIEW, PHASE_OUTCOME]

    def test_no_documents_means_nothing_to_do(self):
        assert _pending_phases(meeting(), args(), None, TODAY) == []

    def test_preview_needs_preview_documents(self):
        m = meeting(minutes=doc())
        assert _pending_phases(m, args(), None, TODAY) == [PHASE_OUTCOME]

    def test_outcome_waits_for_the_minutes(self):
        assert _pending_phases(full_packet(), args(), None, TODAY) == [PHASE_PREVIEW]

    def test_upcoming_meeting_waits_for_its_packet(self):
        # Running early would burn the single shot state gives us.
        upcoming = meeting(day=4, month=8, agenda=doc())
        assert _pending_phases(upcoming, args(), None, TODAY) == []

    def test_upcoming_meeting_runs_once_the_packet_is_posted(self):
        upcoming = full_packet(day=4, month=8)
        assert _pending_phases(upcoming, args(), None, TODAY) == [PHASE_PREVIEW]

    def test_state_skips_a_completed_pass(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet()
        state.record(m.key, PHASE_PREVIEW)
        assert _pending_phases(m, args(), state, TODAY) == []

    def test_force_reruns_a_completed_pass(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet()
        state.record(m.key, PHASE_PREVIEW)
        assert _pending_phases(m, args(force=True), state, TODAY) == [PHASE_PREVIEW]

    def test_phase_flag_narrows_to_one_pass(self):
        m = meeting(agenda=doc(), packet_items=[doc("A001 - x", 2)], minutes=doc("~M", 3))
        assert _pending_phases(m, args(phase=PHASE_OUTCOME), None, TODAY) == [PHASE_OUTCOME]

    def test_a_done_preview_leaves_the_outcome_pending(self, tmp_path):
        state = State.load(tmp_path)
        m = meeting(agenda=doc(), packet_items=[doc("A001 - x", 2)], minutes=doc("~M", 3))
        state.record(m.key, PHASE_PREVIEW)
        assert _pending_phases(m, args(), state, TODAY) == [PHASE_OUTCOME]


class TestSelect:
    def test_newest_meeting_first(self):
        meetings = [full_packet(14), full_packet(28), full_packet(21)]
        jobs = _select(meetings, args(), None, date(2026, 7, 1), TODAY, FakeSource())
        assert [j.meeting.meeting_date.day for j in jobs] == [28, 21, 14]

    def test_respects_the_limit(self):
        meetings = [full_packet(d) for d in (7, 14, 21, 28)]
        jobs = _select(meetings, args(limit=2), None, date(2026, 7, 1), TODAY, FakeSource())
        assert len(jobs) == 2

    def test_a_meeting_needing_both_passes_counts_as_two_jobs(self):
        both = meeting(agenda=doc(), packet_items=[doc("A001 - x", 2)], minutes=doc("~M", 3))
        jobs = _select([both], args(), None, date(2026, 7, 1), TODAY, FakeSource())
        assert len(jobs) == 2

    def test_meetings_before_the_cutoff_are_ignored(self):
        old = full_packet(day=1, month=1)
        jobs = _select([old], args(), None, date(2026, 7, 1), TODAY, FakeSource())
        assert jobs == []

    def test_meetings_past_the_lookahead_horizon_are_ignored(self):
        far = full_packet(day=30, month=9)
        jobs = _select([far], args(), None, date(2026, 7, 1), TODAY, FakeSource())
        assert jobs == []

    def test_documents_are_listed_only_for_real_candidates(self):
        # The date and state filters are free; listing is the bulk of a run's
        # traffic against the city's server.
        old = full_packet(day=1, month=1)
        current = full_packet(28)
        source = FakeSource()
        _select([old, current], args(), None, date(2026, 7, 1), TODAY, source)
        assert source.loaded == [current.key]

    def test_a_fully_digested_meeting_costs_no_listing(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet(28)
        state.record(m.key, PHASE_PREVIEW)
        state.record(m.key, PHASE_OUTCOME)
        source = FakeSource()
        _select([m], args(), state, date(2026, 7, 1), TODAY, source)
        assert source.loaded == []

    def test_an_empty_folder_does_not_consume_a_limit_slot(self):
        # A folder created ahead of its posting: skip it and look further back.
        empty = meeting(28)
        real = full_packet(21)
        jobs = _select([empty, real], args(limit=1), None, date(2026, 7, 1), TODAY,
                       FakeSource())
        assert [j.meeting.meeting_date.day for j in jobs] == [21]

    def test_explicit_meeting_dates_bypass_the_window(self):
        old = full_packet(day=1, month=1)
        jobs = _select([old], args(meeting=[date(2026, 1, 1)]), None,
                       date(2026, 7, 1), TODAY, FakeSource())
        assert [j.phase for j in jobs] == [PHASE_PREVIEW]

    def test_an_explicit_date_with_no_meeting_is_reported_not_crashed(self, caplog):
        jobs = _select([], args(meeting=[date(2026, 1, 1)]), None,
                       date(2026, 7, 1), TODAY, FakeSource())
        assert jobs == []
        assert "no meeting found on 2026-01-01" in caplog.text

    def test_nothing_selected_when_there_is_nothing_to_do(self):
        assert _select([], args(), None, date(2026, 7, 1), TODAY, FakeSource()) == []

    @pytest.mark.parametrize("limit", [0, None])
    def test_a_falsy_limit_means_no_cap(self, limit):
        meetings = [full_packet(d) for d in (7, 14, 21, 28)]
        jobs = _select(meetings, args(limit=limit), None, date(2026, 7, 1), TODAY,
                       FakeSource())
        assert len(jobs) == 4

    def test_job_renders_as_key_and_phase(self):
        jobs = _select([full_packet(28)], args(), None, date(2026, 7, 1), TODAY,
                       FakeSource())
        assert str(jobs[0]) == "city-council-2026-07-28 (preview)"
