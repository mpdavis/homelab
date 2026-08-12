"""Detecting that a meeting we already digested has changed underneath us."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from ames_digest import freshness
from ames_digest.freshness import Policy, diff_manifests, manifest
from ames_digest.meetings import Meeting
from ames_digest.state import PHASE_OUTCOME, PHASE_PREVIEW
from ames_digest.weblink import ENTRY_TYPE_DOCUMENT, Entry

NOW = datetime(2026, 8, 11, 18, 0, 0)


def doc(entry_id, name="A001 - x", modified="8/10/2026 2:13:00 PM", pages=3):
    return Entry(entry_id, name, ENTRY_TYPE_DOCUMENT, "pdf", page_count=pages,
                 last_modified_text=modified)


def meeting(**kwargs):
    stamps = kwargs.pop("stamps", None)
    m = Meeting(board="City Council", meeting_date=date(2026, 8, 11), **kwargs)
    if stamps:
        m.folder_stamps = dict(stamps)
    return m


class TestFingerprint:
    def test_preview_watches_agenda_and_packet(self):
        m = meeting(stamps={"agenda": "A", "packet": "P", "minutes": "M"})
        assert freshness.fingerprint(m, PHASE_PREVIEW) == {"agenda": "A", "packet": "P"}

    def test_outcome_watches_only_the_minutes(self):
        m = meeting(stamps={"agenda": "A", "packet": "P", "minutes": "M"})
        assert freshness.fingerprint(m, PHASE_OUTCOME) == {"minutes": "M"}

    def test_absent_trees_are_omitted_not_blanked(self):
        # A missing key means "no such folder", which must stay distinguishable
        # from a folder that exists and reports an empty stamp.
        m = meeting(stamps={"agenda": "A"})
        assert freshness.fingerprint(m, PHASE_PREVIEW) == {"agenda": "A"}

    def test_no_stamps_at_all(self):
        assert freshness.fingerprint(meeting(), PHASE_PREVIEW) == {}

    def test_a_packet_appearing_later_changes_the_fingerprint(self):
        before = freshness.fingerprint(meeting(stamps={"agenda": "A"}), PHASE_PREVIEW)
        after = freshness.fingerprint(
            meeting(stamps={"agenda": "A", "packet": "P"}), PHASE_PREVIEW
        )
        assert before != after

    def test_unknown_phase_is_empty(self):
        assert freshness.fingerprint(meeting(stamps={"agenda": "A"}), "nonsense") == {}


class TestDocuments:
    def test_preview_reads_agenda_and_items(self):
        m = meeting(agenda=doc(1, "~Master"), packet_items=[doc(2), doc(3)])
        assert [e.entry_id for e in freshness.documents(m, PHASE_PREVIEW)] == [1, 2, 3]

    def test_master_is_excluded_when_items_exist(self):
        # It duplicates the items, so a revision touching only the master is
        # not a change to anything we summarize.
        m = meeting(agenda=doc(1), packet_items=[doc(2)], packet_master=doc(99))
        assert 99 not in [e.entry_id for e in freshness.documents(m, PHASE_PREVIEW)]

    def test_master_is_the_fallback_when_there_are_no_items(self):
        m = meeting(agenda=doc(1), packet_master=doc(99))
        assert [e.entry_id for e in freshness.documents(m, PHASE_PREVIEW)] == [1, 99]

    def test_outcome_reads_only_the_minutes(self):
        m = meeting(agenda=doc(1), packet_items=[doc(2)], minutes=doc(50))
        assert [e.entry_id for e in freshness.documents(m, PHASE_OUTCOME)] == [50]

    def test_nothing_published(self):
        assert freshness.documents(meeting(), PHASE_PREVIEW) == []
        assert freshness.documents(meeting(), PHASE_OUTCOME) == []


class TestManifest:
    def test_shape(self):
        assert manifest([doc(7, "A001 - x", "8/10/2026 2:13:00 PM", 3)]) == {
            "7": {"mod": "8/10/2026 2:13:00 PM", "pages": 3, "name": "A001 - x"}
        }

    def test_keys_are_strings(self):
        # An int key written to state comes back from JSON as a string and
        # would compare unequal to every live entry, reporting the whole packet
        # as new on every run.
        assert all(isinstance(k, str) for k in manifest([doc(7)]))

    def test_empty(self):
        assert manifest([]) == {}


class TestDiffManifests:
    def test_no_change(self):
        before = manifest([doc(1), doc(2)])
        result = diff_manifests(before, dict(before))
        assert not result.changed
        assert sorted(result.unchanged) == ["1", "2"]

    def test_modified_document(self):
        before = manifest([doc(1, modified="8/10/2026 2:13:00 PM")])
        after = manifest([doc(1, modified="8/11/2026 2:12:00 PM")])
        result = diff_manifests(before, after)
        assert result.modified == ["1"]
        assert result.changed

    def test_added_document(self):
        result = diff_manifests(manifest([doc(1)]), manifest([doc(1), doc(2)]))
        assert result.added == ["2"]
        assert result.unchanged == ["1"]

    def test_a_stamp_moving_backwards_is_still_a_change(self):
        # This is why the comparison is string *inequality* rather than
        # ordering: a document restored from an older version, or a clock
        # correction on the repository's side, moves the stamp backwards. An
        # ordering test would call that "not newer" and quietly keep serving
        # the summary of a document that no longer exists.
        before = manifest([doc(1, modified="8/11/2026 2:12:00 PM")])
        after = manifest([doc(1, modified="8/10/2026 2:13:00 PM")])
        result = diff_manifests(before, after)
        assert result.modified == ["1"]
        assert result.changed

    def test_removed_document(self):
        result = diff_manifests(manifest([doc(1), doc(2)]), manifest([doc(1)]))
        assert result.removed == ["2"]
        assert result.changed

    def test_the_live_bug_item_count_unchanged(self):
        # The 2026-08-11 packet: 33 documents created 8/10, three revised 8/11,
        # nothing added or removed. A count check sees no difference at all.
        before = manifest([doc(n, modified="8/10/2026 2:13:00 PM") for n in range(33)])
        after = manifest([
            doc(n, modified="8/11/2026 2:12:00 PM" if n < 3 else "8/10/2026 2:13:00 PM")
            for n in range(33)
        ])
        assert len(before) == len(after)
        result = diff_manifests(before, after)
        assert sorted(result.modified) == ["0", "1", "2"]
        assert len(result.unchanged) == 30

    def test_empty_baseline_reads_as_all_new(self):
        result = diff_manifests({}, manifest([doc(1), doc(2)]))
        assert sorted(result.added) == ["1", "2"]

    @pytest.mark.parametrize("bad", [None, "nope", 42, []])
    def test_malformed_baseline_reads_as_all_new(self, bad):
        assert diff_manifests(bad, manifest([doc(1)])).added == ["1"]

    def test_malformed_entry_reads_as_new(self):
        assert diff_manifests({"1": "not a dict"}, manifest([doc(1)])).added == ["1"]

    def test_resummarize_and_reusable_split(self):
        before = manifest([doc(1), doc(2), doc(3)])
        after = manifest([
            doc(1), doc(2, modified="8/11/2026 9:00:00 AM"), doc(4),
        ])
        result = diff_manifests(before, after)
        assert result.resummarize == {2, 4}
        assert result.reusable == {1}

    def test_buckets_are_sorted_for_stable_logging(self):
        result = diff_manifests({}, manifest([doc(3), doc(1), doc(2)]))
        assert result.added == ["1", "2", "3"]

    def test_summary_string(self):
        result = diff_manifests(manifest([doc(1)]), manifest([doc(2)]))
        assert "1 added" in str(result) and "1 removed" in str(result)


class TestQuietPeriod:
    def test_a_settled_folder(self):
        policy = Policy(quiet_period=timedelta(hours=2), now=NOW)
        assert policy.settled({"packet": "8/11/2026 2:16:00 PM"})

    def test_a_folder_still_being_written(self):
        # The 8/11 packet uploaded over 67 minutes; digesting mid-burst
        # guarantees rework.
        policy = Policy(quiet_period=timedelta(hours=2), now=NOW)
        assert not policy.settled({"packet": "8/11/2026 5:30:00 PM"})

    def test_any_unsettled_tree_holds_the_whole_pass(self):
        policy = Policy(quiet_period=timedelta(hours=2), now=NOW)
        assert not policy.settled({
            "agenda": "8/10/2026 2:00:00 PM", "packet": "8/11/2026 5:30:00 PM",
        })

    def test_zero_disables_the_wait(self):
        policy = Policy(quiet_period=timedelta(0), now=NOW)
        assert policy.settled({"packet": "8/11/2026 5:59:59 PM"})

    def test_unreadable_stamps_fail_open(self):
        # A quiet period that silently does nothing is a missed optimization;
        # one that wedges the pipeline forever is an outage.
        policy = Policy(quiet_period=timedelta(hours=2), now=NOW)
        assert policy.settled({"packet": "not a timestamp"})

    def test_a_future_stamp_fails_open(self, caplog):
        policy = Policy(quiet_period=timedelta(hours=2), now=NOW)
        assert policy.settled({"packet": "8/12/2026 6:00:00 PM"})
        assert "timezone" in caplog.text

    def test_no_clock_supplied_fails_open(self):
        assert Policy(quiet_period=timedelta(hours=2)).settled({"packet": "x"})

    def test_no_stamps_is_settled(self):
        assert Policy(quiet_period=timedelta(hours=2), now=NOW).settled({})

    def test_exactly_at_the_boundary_is_settled(self):
        policy = Policy(quiet_period=timedelta(hours=2), now=NOW)
        assert policy.settled({"packet": "8/11/2026 4:00:00 PM"})


class TestFreeze:
    def _meeting(self, day=11):
        return Meeting(board="City Council", meeting_date=date(2026, 8, day))

    def test_a_published_outcome_freezes_the_meeting(self):
        reason = freshness.frozen_reason(
            self._meeting(), PHASE_PREVIEW,
            outcome_recorded=True, today=date(2026, 8, 11),
        )
        assert reason and "outcome" in reason

    def test_outcome_pass_is_frozen_by_its_own_publication(self):
        reason = freshness.frozen_reason(
            self._meeting(), PHASE_OUTCOME,
            outcome_recorded=True, today=date(2026, 8, 20),
        )
        assert reason is not None

    def test_preview_freezes_once_the_meeting_has_happened(self):
        reason = freshness.frozen_reason(
            self._meeting(day=10), PHASE_PREVIEW,
            outcome_recorded=False, today=date(2026, 8, 11),
        )
        assert reason and "already happened" in reason

    def test_preview_is_revisable_on_the_day_of_the_meeting(self):
        # Materials are routinely revised hours before the meeting starts.
        assert freshness.frozen_reason(
            self._meeting(day=11), PHASE_PREVIEW,
            outcome_recorded=False, today=date(2026, 8, 11),
        ) is None

    def test_preview_is_revisable_before_the_meeting(self):
        assert freshness.frozen_reason(
            self._meeting(day=20), PHASE_PREVIEW,
            outcome_recorded=False, today=date(2026, 8, 11),
        ) is None

    def test_outcome_stays_revisable_until_it_publishes(self):
        # Minutes arrive in pieces — a ~Master and an A001 six hours apart.
        assert freshness.frozen_reason(
            self._meeting(day=1), PHASE_OUTCOME,
            outcome_recorded=False, today=date(2026, 8, 11),
        ) is None
