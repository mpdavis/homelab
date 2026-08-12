"""Which passes a run decides to spend money on.

Every job selected here is a packet download and ~40 model calls, so the rules
about what is skipped matter as much as the rules about what runs.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import pytest

from ames_digest import freshness
from ames_digest.__main__ import _pending_phases, _select, _years_for_window
from ames_digest.meetings import Meeting
from ames_digest.state import PHASE_OUTCOME, PHASE_PREVIEW, State
from ames_digest.weblink import ENTRY_TYPE_DOCUMENT, Entry

TODAY = date(2026, 7, 30)
# Well outside any quiet period relative to the `now` the tests pass in, so
# these meetings read as settled unless a test says otherwise.
SETTLED = "7/20/2026 2:00:00 PM"
NOW = datetime(2026, 7, 30, 12, 0, 0)

POLICY = freshness.Policy(now=NOW)


def doc(name="~Master", entry_id=1, modified=SETTLED):
    return Entry(entry_id, name, ENTRY_TYPE_DOCUMENT, "pdf", last_modified_text=modified)


def args(**overrides):
    base = {"phase": "both", "force": False, "recheck": False, "meeting": None,
            "limit": 3, "lookahead_days": 10}
    base.update(overrides)
    return argparse.Namespace(**base)


def meeting(day=28, month=7, stamps=None, **kwargs):
    kwargs.setdefault("board", "City Council")
    m = Meeting(meeting_date=date(2026, month, day), **kwargs)
    m.folder_stamps = dict(
        stamps if stamps is not None
        else {"agenda": SETTLED, "packet": SETTLED, "minutes": SETTLED}
    )
    return m


def full_packet(day=28, month=7, **kwargs):
    return meeting(day, month, agenda=doc(), packet_items=[doc("A001 - x", 2)], **kwargs)


def phases(jobs):
    return [job.phase for job in jobs]


def baseline(state, m, phase, **extra):
    """Record a pass the way a real run does — with its freshness baseline."""
    state.record(
        m.key, phase,
        folders=freshness.fingerprint(m, phase),
        documents=freshness.manifest(freshness.documents(m, phase)),
        **extra,
    )


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
        assert phases(_pending_phases(m, args(), None, TODAY, POLICY)) == [PHASE_PREVIEW, PHASE_OUTCOME]

    def test_no_documents_means_nothing_to_do(self):
        assert phases(_pending_phases(meeting(), args(), None, TODAY, POLICY)) == []

    def test_preview_needs_preview_documents(self):
        m = meeting(minutes=doc())
        assert phases(_pending_phases(m, args(), None, TODAY, POLICY)) == [PHASE_OUTCOME]

    def test_outcome_waits_for_the_minutes(self):
        assert phases(_pending_phases(full_packet(), args(), None, TODAY, POLICY)) == [PHASE_PREVIEW]

    def test_upcoming_meeting_waits_for_its_packet(self):
        # Running early would burn the single shot state gives us.
        upcoming = meeting(day=4, month=8, agenda=doc())
        assert phases(_pending_phases(upcoming, args(), None, TODAY, POLICY)) == []

    def test_upcoming_meeting_runs_once_the_packet_is_posted(self):
        upcoming = full_packet(day=4, month=8)
        assert phases(_pending_phases(upcoming, args(), None, TODAY, POLICY)) == [PHASE_PREVIEW]

    def test_state_skips_a_completed_pass(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet()
        state.record(m.key, PHASE_PREVIEW)
        assert phases(_pending_phases(m, args(), state, TODAY, POLICY)) == []

    def test_force_reruns_a_completed_pass(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet()
        state.record(m.key, PHASE_PREVIEW)
        assert phases(_pending_phases(m, args(force=True), state, TODAY, POLICY)) == [PHASE_PREVIEW]

    def test_phase_flag_narrows_to_one_pass(self):
        m = meeting(agenda=doc(), packet_items=[doc("A001 - x", 2)], minutes=doc("~M", 3))
        assert phases(_pending_phases(m, args(phase=PHASE_OUTCOME), None, TODAY, POLICY)) == [PHASE_OUTCOME]

    def test_a_done_preview_leaves_the_outcome_pending(self, tmp_path):
        state = State.load(tmp_path)
        m = meeting(agenda=doc(), packet_items=[doc("A001 - x", 2)], minutes=doc("~M", 3))
        state.record(m.key, PHASE_PREVIEW)
        assert phases(_pending_phases(m, args(), state, TODAY, POLICY)) == [PHASE_OUTCOME]


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

    def test_a_fully_digested_unchanged_meeting_costs_no_listing(self, tmp_path):
        # Layer 1: the folder stamps came free with discovery, so a meeting
        # that has not moved is dismissed without listing anything. This is
        # what keeps a no-op poll at nine requests.
        state = State.load(tmp_path)
        m = full_packet(28, minutes=doc("~M", 3))
        baseline(state, m, PHASE_PREVIEW)
        baseline(state, m, PHASE_OUTCOME)
        source = FakeSource()
        _select([m], args(), state, date(2026, 7, 1), TODAY, source, POLICY)
        assert source.loaded == []

    def test_a_record_with_no_fingerprint_is_re_examined_once(self, tmp_path):
        # A v2 record predates freshness, so it has no baseline to compare
        # against. Those are exactly the digests that may have caught a packet
        # mid-upload, so they get looked at once more.
        state = State.load(tmp_path)
        m = full_packet(28, minutes=doc("~M", 3))
        state.record(m.key, PHASE_PREVIEW)
        state.record(m.key, PHASE_OUTCOME)
        source = FakeSource()
        _select([m], args(), state, date(2026, 7, 1), TODAY, source, POLICY)
        assert source.loaded == [m.key]

    def test_a_moved_folder_costs_a_listing(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet(28, minutes=doc("~M", 3))
        baseline(state, m, PHASE_PREVIEW)
        baseline(state, m, PHASE_OUTCOME)
        m.folder_stamps["packet"] = "7/29/2026 9:00:00 AM"
        source = FakeSource()
        _select([m], args(), state, date(2026, 7, 1), TODAY, source, POLICY)
        assert source.loaded == [m.key]

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


class TestRevisions:
    """Layer 2 and the policy over it: when a changed meeting is digested again."""

    # Dated today, not in the past: a preview freezes once its meeting has
    # happened, so a revisable meeting is one still ahead of the clock. This is
    # also the live case — the 8/11 packet was revised hours before the meeting.
    DAY = TODAY.day

    def _digested(self, tmp_path, **kwargs):
        state = State.load(tmp_path)
        m = full_packet(self.DAY, **kwargs)
        baseline(state, m, PHASE_PREVIEW)
        return state, m

    def _revise(self, m, modified="7/29/2026 9:00:00 AM"):
        """Move one packet document, the way the clerk revises in place."""
        m.packet_items = [doc("A001 - x", 2, modified=modified)]
        m.folder_stamps["packet"] = modified
        return m

    def test_an_unchanged_meeting_is_not_re_digested(self, tmp_path):
        state, m = self._digested(tmp_path)
        assert _pending_phases(m, args(), state, TODAY, POLICY) == []

    def test_a_revised_document_produces_a_revision_job(self, tmp_path):
        state, m = self._digested(tmp_path)
        self._revise(m)
        jobs = _pending_phases(m, args(), state, TODAY, POLICY)
        assert phases(jobs) == [PHASE_PREVIEW]
        assert jobs[0].is_revision
        assert jobs[0].diff.modified == ["2"]

    def test_an_added_document_produces_a_revision_job(self, tmp_path):
        state, m = self._digested(tmp_path)
        m.packet_items = [doc("A001 - x", 2), doc("A002 - y", 3)]
        m.folder_stamps["packet"] = "7/29/2026 9:00:00 AM"
        jobs = _pending_phases(m, args(), state, TODAY, POLICY)
        assert jobs[0].diff.added == ["3"]
        # The agenda (1) is unchanged too, and is reusable on the same terms.
        assert jobs[0].diff.reusable == {1, 2}

    def test_a_touched_folder_with_no_document_change_costs_no_model_call(self, tmp_path):
        state, m = self._digested(tmp_path)
        m.folder_stamps["packet"] = "7/29/2026 9:00:00 AM"   # folder only
        assert _pending_phases(m, args(), state, TODAY, POLICY) == []

    def test_that_false_positive_is_re_baselined_so_it_stops_recurring(self, tmp_path):
        # Without writing the new stamp back, this meeting would fail layer 1
        # on every poll forever and pay a listing each time.
        state, m = self._digested(tmp_path)
        m.folder_stamps["packet"] = "7/29/2026 9:00:00 AM"
        _pending_phases(m, args(), state, TODAY, POLICY)
        assert state.entry(m.key, PHASE_PREVIEW)["folders"]["packet"] == (
            "7/29/2026 9:00:00 AM"
        )
        # And the next poll agrees there is nothing to do.
        assert _pending_phases(m, args(), state, TODAY, POLICY) == []

    def test_a_revision_waits_for_the_folder_to_settle(self, tmp_path):
        state, m = self._digested(tmp_path)
        self._revise(m, modified="7/30/2026 11:30:00 AM")   # 30 minutes before NOW
        assert _pending_phases(m, args(), state, TODAY, POLICY) == []

    def test_a_past_meeting_with_a_published_outcome_is_never_re_digested(self, tmp_path):
        state, m = self._digested(tmp_path, minutes=doc("~M", 3))
        baseline(state, m, PHASE_OUTCOME)
        self._revise(m)
        assert _pending_phases(m, args(), state, TODAY, POLICY) == []

    def test_a_preview_freezes_once_the_meeting_has_happened(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet(day=20, month=7)          # before TODAY
        baseline(state, m, PHASE_PREVIEW)
        self._revise(m)
        assert _pending_phases(m, args(), state, TODAY, POLICY) == []

    def test_recheck_overrides_a_freeze(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet(day=20, month=7)
        baseline(state, m, PHASE_PREVIEW)
        self._revise(m)
        assert phases(_pending_phases(m, args(recheck=True), state, TODAY, POLICY)) == [
            PHASE_PREVIEW
        ]

    def test_recheck_still_needs_a_real_change(self, tmp_path):
        # Unlike --force, which re-digests regardless.
        state = State.load(tmp_path)
        m = full_packet(day=20, month=7)
        baseline(state, m, PHASE_PREVIEW)
        assert _pending_phases(m, args(recheck=True), state, TODAY, POLICY) == []

    def test_the_revision_cap_stops_a_runaway(self, tmp_path, caplog):
        state = State.load(tmp_path)
        m = full_packet(self.DAY)
        baseline(state, m, PHASE_PREVIEW, revision=5)
        self._revise(m)
        assert _pending_phases(m, args(), state, TODAY, POLICY) == []
        assert "cap 5" in caplog.text and m.key in caplog.text

    def test_under_the_cap_still_revises(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet(self.DAY)
        baseline(state, m, PHASE_PREVIEW, revision=4)
        self._revise(m)
        assert len(_pending_phases(m, args(), state, TODAY, POLICY)) == 1

    def test_recheck_overrides_the_cap(self, tmp_path):
        state = State.load(tmp_path)
        m = full_packet(self.DAY)
        baseline(state, m, PHASE_PREVIEW, revision=99)
        self._revise(m)
        assert len(_pending_phases(m, args(recheck=True), state, TODAY, POLICY)) == 1

    def test_revised_minutes_produce_an_outcome_revision(self, tmp_path):
        # The minutes folder received ~Master and A001 six hours apart.
        state = State.load(tmp_path)
        m = full_packet(28, minutes=doc("~M", 3))
        baseline(state, m, PHASE_PREVIEW)
        baseline(state, m, PHASE_OUTCOME)
        m.minutes = doc("~M", 3, modified="7/29/2026 9:00:00 AM")
        m.folder_stamps["minutes"] = "7/29/2026 9:00:00 AM"
        # The outcome is published, so the meeting is frozen — only --recheck
        # reaches it, which is the documented escape hatch.
        assert phases(_pending_phases(m, args(recheck=True), state, TODAY, POLICY)) == [
            PHASE_OUTCOME
        ]


class TestQuietPeriodOnFirstDigest:
    def test_a_meeting_mid_upload_is_not_digested(self):
        m = full_packet(28)
        m.folder_stamps["packet"] = "7/30/2026 11:30:00 AM"    # 30 min before NOW
        assert _pending_phases(m, args(), None, TODAY, POLICY) == []

    def test_it_is_digested_once_the_folder_settles(self):
        m = full_packet(28)
        m.folder_stamps["packet"] = "7/30/2026 8:00:00 AM"     # 4 hours before NOW
        assert phases(_pending_phases(m, args(), None, TODAY, POLICY)) == [PHASE_PREVIEW]

    def test_disabling_the_quiet_period_digests_immediately(self):
        m = full_packet(28)
        m.folder_stamps["packet"] = "7/30/2026 11:59:00 AM"
        policy = freshness.Policy(quiet_period=timedelta(0), now=NOW)
        assert phases(_pending_phases(m, args(), None, TODAY, policy)) == [PHASE_PREVIEW]
