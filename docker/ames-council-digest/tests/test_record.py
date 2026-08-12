"""The JSON meeting record is the durable input to future renderers."""

from datetime import date, datetime
import json

from ames_digest.agenda import AgendaOutline
from ames_digest.llm import Usage
from ames_digest.meetings import Meeting
from ames_digest.record import MeetingRecord
from ames_digest.summarize import ItemSummary, MeetingDigest


def digest(kind="preview"):
    meeting = Meeting(board="City Council", meeting_date=date(2026, 7, 28))
    return MeetingDigest(
        meeting=meeting,
        body_markdown=f"## {kind}",
        kind=kind,
        agenda=AgendaOutline(meeting_time="6:00 PM", location="City Hall"),
        items=[ItemSummary(code="A001", title="Claims", entry_id=7, url="u", summary="s")],
        agenda_url="agenda",
        packet_url="packet",
        minutes_url="minutes" if kind == "outcome" else None,
        generated_at=datetime(2026, 7, 28, 18, 0),
        usage=Usage(10, 20, 2),
        model="model",
        revision=1,
    )


def test_record_contains_everything_needed_by_a_renderer(tmp_path):
    record = MeetingRecord.from_digest(
        digest(),
        documents={"7": {"mod": "today", "pages": 2, "name": "A001"}},
        revised_at=["2026-07-28T17:00:00"],
    )
    path = record.save(tmp_path)
    payload = json.loads(path.read_text())

    assert payload["version"] == 1
    assert payload["meeting"]["date"] == "2026-07-28"
    assert payload["items"][0]["entry_id"] == 7
    assert payload["passes"]["preview"]["body"] == "## preview"
    assert payload["passes"]["preview"]["documents"]["7"]["mod"] == "today"
    assert MeetingRecord.load(tmp_path, record.key).to_dict() == payload


def test_outcome_keeps_the_preview_body():
    preview = MeetingRecord.from_digest(digest())
    outcome = MeetingRecord.from_digest(
        digest("outcome"), prior=preview, preview_body=preview.preview["body"]
    )

    assert outcome.preview["body"] == "## preview"
    assert outcome.outcome["body"] == "## outcome"


def test_invalid_record_is_ignored(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps([]))
    assert MeetingRecord.load(tmp_path, "bad") is None
