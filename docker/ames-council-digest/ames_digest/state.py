"""Which meetings have already been digested.

A single JSON file on the PVC. Written atomically so a pod evicted mid-write
leaves the previous state intact rather than a truncated file that would make
the next run re-digest (and re-bill) everything.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILENAME = "processed.json"
STATE_VERSION = 1


@dataclass
class State:
    path: Path
    processed: dict[str, dict]

    @classmethod
    def load(cls, state_dir: Path) -> "State":
        path = state_dir / STATE_FILENAME
        if not path.exists():
            return cls(path=path, processed={})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Treat a corrupt state file as empty but keep it on disk for
            # inspection — losing state costs money, so make the loss visible.
            log.error("state file %s unreadable (%s); starting empty", path, exc)
            return cls(path=path, processed={})
        return cls(path=path, processed=payload.get("processed") or {})

    def seen(self, key: str) -> bool:
        return key in self.processed

    def record(self, key: str, **details: object) -> None:
        self.processed[key] = {
            "digested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **details,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STATE_VERSION, "processed": self.processed}
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".processed-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
