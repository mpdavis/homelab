"""What a completed pass writes back — the ledger and the freshness baseline.

A revision re-summarizes a fraction of the packet, so it spends a fraction of
the original. Both the state ledger and the archive have to accumulate rather
than replace, or the index's cumulative spend *falls* after a revision and the
page's footer stops reporting what it actually cost.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ames_digest import archive, freshness
from ames_digest.__main__ import Job, _archive_preview, _record_pass
from ames_digest.agenda import AgendaOutline
from ames_digest.config import Config
from ames_digest.llm import Usage
from ames_digest.meetings import Meeting
from ames_digest.state import PHASE_PREVIEW, State
from ames_digest.summarize import ItemSummary, MeetingDigest
from ames_digest.weblink import ENTRY_TYPE_DOCUMENT, Entry

STAMP = "8/11/2026 2:16:54 PM"


def doc(entry_id=2, name="A001 - x", modified=STAMP):
    return Entry(entry_id, name, ENTRY_TYPE_DOCUMENT, "pdf", page_count=3,
                 last_modified_text=modified)


def meeting():
    m = Meeting(board="City Council", meeting_date=date(2026, 8, 11),
                agenda=doc(1, "~Master"), packet_items=[doc(2)])
    m.folder_stamps = {"agenda": STAMP, "packet": STAMP}
    return m


def digest(m, usage=Usage(100, 20, 5), prior=Usage(), revision=0):
    return MeetingDigest(
        meeting=m,
        body_markdown="# page",
        items=[ItemSummary(code="A001", title="x", entry_id=2, url="u", summary="s")],
        agenda=AgendaOutline(items=[], meeting_time="6:00 PM"),
        usage=usage,
        prior_usage=prior,
        revision=revision,
        model="claude-sonnet-4-5",
        generated_at=datetime(2026, 8, 11, 15, 0, 0),
    )


class TestRecordPass:
    def test_a_first_digest_records_its_own_spend(self, tmp_path):
        state = State.load(tmp_path)
        m = meeting()
        _record_pass(state, Job(m, PHASE_PREVIEW), digest(m))
        record = state.entry(m.key, PHASE_PREVIEW)
        assert record["input_tokens"] == 100
        assert record["calls"] == 5
        assert record["revision"] == 0
        assert record["revised_at"] == []

    def test_a_first_digest_stores_its_freshness_baseline(self, tmp_path):
        state = State.load(tmp_path)
        m = meeting()
        _record_pass(state, Job(m, PHASE_PREVIEW), digest(m))
        record = state.entry(m.key, PHASE_PREVIEW)
        assert record["folders"] == {"agenda": STAMP, "packet": STAMP}
        assert set(record["documents"]) == {"1", "2"}
        assert record["documents"]["2"]["mod"] == STAMP

    def test_a_revision_accumulates_tokens(self, tmp_path):
        # The whole point: 294k for the original, 8k for the revision, and the
        # ledger has to read 302k rather than 8k.
        state = State.load(tmp_path)
        m = meeting()
        _record_pass(state, Job(m, PHASE_PREVIEW), digest(m, usage=Usage(294_000, 33_000, 41)))
        _record_pass(
            state,
            Job(m, PHASE_PREVIEW, diff=freshness.ManifestDiff(modified=["2"])),
            digest(m, usage=Usage(8_000, 900, 4)),
        )
        record = state.entry(m.key, PHASE_PREVIEW)
        assert record["input_tokens"] == 302_000
        assert record["output_tokens"] == 33_900
        assert record["calls"] == 45

    def test_a_revision_counts_and_timestamps_itself(self, tmp_path):
        state = State.load(tmp_path)
        m = meeting()
        job = Job(m, PHASE_PREVIEW, diff=freshness.ManifestDiff(modified=["2"]))
        _record_pass(state, Job(m, PHASE_PREVIEW), digest(m))
        _record_pass(state, job, digest(m))
        _record_pass(state, job, digest(m))
        record = state.entry(m.key, PHASE_PREVIEW)
        assert record["revision"] == 2
        assert len(record["revised_at"]) == 2

    def test_a_forced_re_digest_replaces_rather_than_accumulates(self, tmp_path):
        # --force is not a revision: it re-buys the same pass outright, so
        # adding its spend to the previous run's would double-count it.
        state = State.load(tmp_path)
        m = meeting()
        _record_pass(state, Job(m, PHASE_PREVIEW), digest(m, usage=Usage(100, 20, 5)))
        _record_pass(state, Job(m, PHASE_PREVIEW), digest(m, usage=Usage(100, 20, 5)))
        record = state.entry(m.key, PHASE_PREVIEW)
        assert record["input_tokens"] == 100
        assert record["revision"] == 0

    def test_the_baseline_is_refreshed_by_a_revision(self, tmp_path):
        state = State.load(tmp_path)
        m = meeting()
        _record_pass(state, Job(m, PHASE_PREVIEW), digest(m))
        m.packet_items = [doc(2, modified="8/12/2026 9:00:00 AM")]
        m.folder_stamps["packet"] = "8/12/2026 9:00:00 AM"
        _record_pass(
            state,
            Job(m, PHASE_PREVIEW, diff=freshness.ManifestDiff(modified=["2"])),
            digest(m),
        )
        record = state.entry(m.key, PHASE_PREVIEW)
        assert record["folders"]["packet"] == "8/12/2026 9:00:00 AM"
        assert record["documents"]["2"]["mod"] == "8/12/2026 9:00:00 AM"

    def test_a_malformed_prior_record_does_not_crash_the_carry(self, tmp_path):
        state = State.load(tmp_path)
        m = meeting()
        state.record(m.key, PHASE_PREVIEW, input_tokens="lots", revision="many",
                     revised_at="nope")
        _record_pass(
            state,
            Job(m, PHASE_PREVIEW, diff=freshness.ManifestDiff(modified=["2"])),
            digest(m, usage=Usage(100, 20, 5)),
        )
        record = state.entry(m.key, PHASE_PREVIEW)
        assert record["input_tokens"] == 100
        assert record["revision"] == 1
        assert isinstance(record["revised_at"], list)


class TestArchivePreview:
    def _cfg(self, tmp_path):
        cfg = Config()
        cfg.state_dir = tmp_path
        return cfg

    def test_writes_the_page_and_items(self, tmp_path):
        m = meeting()
        _archive_preview(self._cfg(tmp_path), digest(m))
        loaded = archive.load_preview(tmp_path, m.key)
        assert loaded.body == "# page"
        assert loaded.items[0]["entry_id"] == 2

    def test_stores_the_pages_cumulative_spend_not_the_passs(self, tmp_path):
        # A revision that spent 8k on top of an original 294k must archive
        # 302k, or the next pass reads 8k as the page's whole history.
        m = meeting()
        _archive_preview(
            self._cfg(tmp_path),
            digest(m, usage=Usage(8_000, 900, 4), prior=Usage(294_000, 33_000, 41),
                   revision=1),
        )
        loaded = archive.load_preview(tmp_path, m.key)
        assert loaded.usage.input_tokens == 302_000
        assert loaded.usage.calls == 45

    def test_carries_the_revision_count(self, tmp_path):
        m = meeting()
        _archive_preview(self._cfg(tmp_path), digest(m, revision=3))
        assert archive.load_preview(tmp_path, m.key).revision == 3

    def test_a_first_digest_archives_its_own_spend(self, tmp_path):
        m = meeting()
        _archive_preview(self._cfg(tmp_path), digest(m, usage=Usage(100, 20, 5)))
        assert archive.load_preview(tmp_path, m.key).usage.input_tokens == 100

    def test_the_agenda_outline_round_trips(self, tmp_path):
        m = meeting()
        _archive_preview(self._cfg(tmp_path), digest(m))
        loaded = archive.load_preview(tmp_path, m.key)
        assert AgendaOutline.from_archive(loaded.agenda).meeting_time == "6:00 PM"
