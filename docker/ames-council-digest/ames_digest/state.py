"""Which meetings have been digested, and in which pass.

A meeting is digested twice: once from the agenda and packet before it happens
(``preview``), and once from the minutes afterwards (``outcome``). They are
tracked separately because the documents arrive days apart — a meeting whose
preview is done still has an outcome pending for a week or more.

A single JSON file on the PVC, written atomically so a pod evicted mid-write
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
STATE_VERSION = 2

PHASE_PREVIEW = "preview"
PHASE_OUTCOME = "outcome"
PHASES = (PHASE_PREVIEW, PHASE_OUTCOME)


def _migrate(payload: dict) -> dict[str, dict]:
    """Bring an on-disk payload up to the current schema.

    v1 stored one flat record per meeting, which was always a preview — the
    outcome pass didn't exist. Re-keying those under ``preview`` is what keeps
    an upgrade from re-summarizing (and re-paying for) every packet on disk.
    """
    processed = payload.get("processed") or {}
    version = payload.get("version")

    if version == STATE_VERSION:
        return processed

    if version == 1:
        migrated = {
            key: {PHASE_PREVIEW: record}
            for key, record in processed.items()
            if isinstance(record, dict)
        }
        log.info("migrated %d state entries from v1 to v2", len(migrated))
        return migrated

    # An unknown (newer) version: keep whatever is already phase-shaped rather
    # than discarding it, since dropping state costs real money.
    log.warning("unrecognized state version %r; salvaging phase-shaped entries", version)
    return {
        key: record
        for key, record in processed.items()
        if isinstance(record, dict) and any(p in record for p in PHASES)
    }


@dataclass
class Totals:
    """Cumulative model usage across every digest this volume has produced."""

    digests: int = 0
    meetings: int = 0
    previews: int = 0
    outcomes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    # Records written before calls were tracked contribute tokens but no call
    # count, so the total would otherwise read as an undercount with no
    # explanation.
    records_missing_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self, input_per_mtok: float, output_per_mtok: float) -> float:
        return (
            self.input_tokens * input_per_mtok + self.output_tokens * output_per_mtok
        ) / 1_000_000


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
        return cls(path=path, processed=_migrate(payload))

    def seen(self, key: str, phase: str = PHASE_PREVIEW) -> bool:
        return phase in (self.processed.get(key) or {})

    def record(self, key: str, phase: str = PHASE_PREVIEW, **details: object) -> None:
        entry = self.processed.setdefault(key, {})
        entry[phase] = {
            "digested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **details,
        }

    def totals(self) -> Totals:
        """Sum usage over every recorded pass.

        The state file is the only complete ledger — a digest's own footer
        reports one pass, and the rendered files can be deleted without the
        spend being undone.
        """
        totals = Totals(meetings=len(self.processed))
        for phases in self.processed.values():
            if not isinstance(phases, dict):
                continue
            for phase, record in phases.items():
                if not isinstance(record, dict):
                    continue
                totals.digests += 1
                if phase == PHASE_PREVIEW:
                    totals.previews += 1
                elif phase == PHASE_OUTCOME:
                    totals.outcomes += 1
                totals.input_tokens += int(record.get("input_tokens") or 0)
                totals.output_tokens += int(record.get("output_tokens") or 0)
                if "calls" in record:
                    totals.calls += int(record.get("calls") or 0)
                else:
                    totals.records_missing_calls += 1
        return totals

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
