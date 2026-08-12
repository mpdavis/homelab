"""The durable, re-renderable record for one meeting."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECORD_VERSION = 1


@dataclass
class MeetingRecord:
    """All structured data needed to render a meeting without an LLM."""

    key: str
    board: str
    meeting_date: str
    label: str = ""
    agenda: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    meeting: dict[str, Any] = field(default_factory=dict)
    preview: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": RECORD_VERSION,
            "key": self.key,
            "meeting": {
                "board": self.board,
                "date": self.meeting_date,
                "label": self.label,
                **self.meeting,
            },
            "agenda": self.agenda,
            "items": self.items,
            "passes": {"preview": self.preview, "outcome": self.outcome},
        }

    def save(self, output_dir: Path) -> Path:
        """Atomically write the record next to the rendered meeting files."""
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{self.key}.json"
        fd, temporary = tempfile.mkstemp(dir=str(output_dir), prefix=f".{self.key}-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return path

    @classmethod
    def load(cls, output_dir: Path, key: str) -> "MeetingRecord | None":
        path = output_dir / f"{key}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != RECORD_VERSION:
            return None
        meeting = payload.get("meeting")
        passes = payload.get("passes")
        if not isinstance(meeting, dict) or not isinstance(passes, dict):
            return None
        return cls(
            key=str(payload.get("key") or key),
            board=str(meeting.get("board") or ""),
            meeting_date=str(meeting.get("date") or ""),
            label=str(meeting.get("label") or ""),
            meeting={k: v for k, v in meeting.items() if k not in {"board", "date", "label"}},
            agenda=payload.get("agenda") if isinstance(payload.get("agenda"), dict) else {},
            items=payload.get("items") if isinstance(payload.get("items"), list) else [],
            preview=passes.get("preview") if isinstance(passes.get("preview"), dict) else None,
            outcome=passes.get("outcome") if isinstance(passes.get("outcome"), dict) else None,
        )

    @classmethod
    def from_digest(
        cls,
        digest: Any,
        *,
        documents: dict[str, dict[str, Any]] | None = None,
        revised_at: list[str] | None = None,
        prior: "MeetingRecord | None" = None,
        preview_body: str | None = None,
    ) -> "MeetingRecord":
        meeting = digest.meeting
        record = prior or cls(
            key=meeting.key,
            board=meeting.board,
            meeting_date=meeting.meeting_date.isoformat(),
            label=meeting.label,
        )
        record.key = meeting.key
        record.board = meeting.board
        record.meeting_date = meeting.meeting_date.isoformat()
        record.label = meeting.label
        record.agenda = digest.agenda.to_archive()
        record.items = [item.to_archive() for item in digest.items]
        record.meeting = {
            "agenda_url": digest.agenda_url,
            "packet_url": digest.packet_url,
            "minutes_url": digest.minutes_url,
        }
        pass_record = {
            "body": digest.body_markdown,
            "generated_at": digest.generated_at.isoformat(timespec="seconds"),
            "revision": digest.revision,
            "revised_at": list(revised_at or []),
            "documents": documents or {},
            "model": digest.model,
            "usage": {
                "input_tokens": digest.total_usage.input_tokens,
                "output_tokens": digest.total_usage.output_tokens,
                "calls": digest.total_usage.calls,
            },
        }
        if digest.is_outcome:
            record.outcome = pass_record
            if record.preview is None and preview_body:
                record.preview = {"body": preview_body}
        else:
            record.preview = pass_record
        return record
