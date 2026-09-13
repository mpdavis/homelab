"""Background refresh, inside the server process.

DuckDB takes one writer, so the usual homelab shape — a CronJob writing to a
PVC the web pod also mounts — is not available. Instead the server owns the
database and runs its own schedule on a daemon thread.

That is a real constraint and worth naming: a wedged refresh takes the web UI's
freshness with it, and there is no separate Job whose failure would show up in
`kubectl get jobs`. In exchange, there is exactly one process that can write,
which is the property the storage engine requires. The refresh reports its last
outcome on the status page and through `/healthz`, which is how a wedged run
becomes visible.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import current_season, settings

log = logging.getLogger(__name__)


@dataclass
class RefreshState:
    """What the last few refresh attempts did, for the status page."""

    last_odds_at: datetime | None = None
    last_odds_rows: int = 0
    last_ingest_at: datetime | None = None
    last_ingest_summary: str = ""
    last_error: str = ""
    running: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        return {
            "last_odds_at": self.last_odds_at.isoformat() if self.last_odds_at else None,
            "last_odds_rows": self.last_odds_rows,
            "last_ingest_at": (
                self.last_ingest_at.isoformat() if self.last_ingest_at else None
            ),
            "last_ingest_summary": self.last_ingest_summary,
            "last_error": self.last_error,
            "running": self.running,
        }


STATE = RefreshState()


def refresh_odds_once() -> int:
    from .ingest import refresh_live_odds

    with STATE.lock:
        STATE.running = True
    try:
        rows = refresh_live_odds()
        STATE.last_odds_at = datetime.now(timezone.utc)
        STATE.last_odds_rows = rows
        STATE.last_error = ""
        return rows
    except Exception as exc:  # noqa: BLE001 — the thread must survive
        log.exception("Live odds refresh failed")
        STATE.last_error = f"odds: {exc}"
        return 0
    finally:
        with STATE.lock:
            STATE.running = False


def _is_empty() -> bool:
    """True when no games have ever been ingested."""
    from .db import cursor

    with cursor() as conn:
        return conn.execute("SELECT count(*) FROM games").fetchone()[0] == 0


def refresh_data_once(seasons: list[int] | None = None) -> str:
    """Re-ingest and rebuild features.

    Defaults to the current season, which is all a routine refresh needs. The
    exception is an empty database: there, the default would leave the service
    permanently holding one season, and every backtest would have nothing to
    train on. So a first run backfills the whole configured range instead.

    That matters more than it looks, because this process is the *only* thing
    that can write. DuckDB's lock is exclusive — a second process running
    `gridiron ingest` against the same file cannot even open it read-only — so
    there is no shell-in-and-seed-it fallback. If this function does not do the
    backfill, nothing does.
    """
    from .config import season_range
    from .db import cursor
    from .features.build import build_team_game
    from .ingest import ingest_seasons

    with STATE.lock:
        STATE.running = True
    try:
        if seasons is None:
            if _is_empty():
                first, last = season_range()
                seasons = list(range(first, last + 1))
                log.info("Empty database; backfilling seasons %d-%d", first, last)
            else:
                seasons = [current_season()]
        report = ingest_seasons(seasons)
        with cursor() as conn:
            build_team_game(conn, seasons)
        STATE.last_ingest_at = datetime.now(timezone.utc)
        STATE.last_ingest_summary = report.summary()
        if report.errors:
            STATE.last_error = "; ".join(report.errors[:3])
        else:
            STATE.last_error = ""
        return report.summary()
    except Exception as exc:  # noqa: BLE001
        log.exception("Data refresh failed")
        STATE.last_error = f"ingest: {exc}"
        return str(exc)
    finally:
        with STATE.lock:
            STATE.running = False


def start(
    odds_interval_minutes: int = 30, data_interval_hours: int = 6
) -> threading.Thread | None:
    """Start the refresh loop, unless there is nothing configured to refresh."""
    cfg = settings()
    if not (cfg.has_cfbd or cfg.has_odds_api):
        log.info("No API keys configured; background refresh not started")
        return None

    def loop() -> None:
        # Nothing on startup: a rolling restart would otherwise fire an ingest
        # per replica per deploy, and the first scheduled tick is soon enough.
        next_odds = time.monotonic() + 60
        next_data = time.monotonic() + 300
        while True:
            now = time.monotonic()
            if cfg.has_odds_api and now >= next_odds:
                refresh_odds_once()
                next_odds = now + odds_interval_minutes * 60
            if cfg.has_cfbd and now >= next_data:
                refresh_data_once()
                next_data = now + data_interval_hours * 3600
            time.sleep(20)

    thread = threading.Thread(target=loop, name="gridiron-refresh", daemon=True)
    thread.start()
    log.info(
        "Background refresh started (odds every %dm, data every %dh)",
        odds_interval_minutes,
        data_interval_hours,
    )
    return thread
