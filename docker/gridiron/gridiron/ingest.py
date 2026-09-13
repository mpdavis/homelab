"""Pull upstream data into the store.

Idempotent by season: re-ingesting 2023 deletes 2023's rows and reloads them,
so a partial run is repaired by running it again rather than by reasoning about
what got through. That matters because a full backfill is long enough to be
interrupted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import current_season, season_range, settings
from .db import cursor, replace_rows
from .sources.cfbd import CFBDClient
from .sources.oddsapi import OddsAPIClient, TeamMatcher

log = logging.getLogger(__name__)

# The order matters only for `teams`, which the live-odds matcher depends on.
DATASETS = ("teams", "games", "drives", "plays", "lines", "talent", "recruiting", "portal")


@dataclass
class IngestReport:
    seasons: list[int] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add(self, dataset: str, count: int) -> None:
        self.rows[dataset] = self.rows.get(dataset, 0) + count

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(self.rows.items()))
        span = (
            f"{min(self.seasons)}-{max(self.seasons)}" if self.seasons else "none"
        )
        status = "ok" if self.ok else f"{len(self.errors)} error(s)"
        return f"seasons {span}: {counts} [{status}]"


def ingest_seasons(
    seasons: list[int] | None = None,
    datasets: tuple[str, ...] = DATASETS,
    *,
    skip_plays_for_complete_seasons: bool = True,
) -> IngestReport:
    """Backfill or refresh the source tables.

    ``skip_plays_for_complete_seasons`` is the difference between a five-minute
    weekly refresh and a two-hour one. Play-by-play for a finished season never
    changes, so once a season's play count is non-zero and every one of its
    games is marked complete, it is not re-downloaded. Pass False to force it.
    """
    report = IngestReport()
    if seasons is None:
        first, last = season_range()
        seasons = list(range(first, last + 1))
    report.seasons = list(seasons)

    with CFBDClient() as client:
        for season in seasons:
            log.info("Ingesting season %s", season)
            for dataset in datasets:
                try:
                    count = _ingest_one(
                        client, season, dataset, skip_plays_for_complete_seasons
                    )
                    report.add(dataset, count)
                except Exception as exc:  # noqa: BLE001 — one bad season must
                    # not abandon the other nine; the error is reported instead.
                    message = f"{season}/{dataset}: {exc}"
                    log.exception("Ingest failed for %s", message)
                    report.errors.append(message)
    return report


def _ingest_one(
    client: CFBDClient, season: int, dataset: str, skip_settled_plays: bool
) -> int:
    with cursor() as conn:
        if dataset == "teams":
            # Not season-partitioned: the table is a current-membership
            # snapshot, so the newest season ingested wins and the whole table
            # is replaced. Guarded on a non-empty response so a failed call
            # cannot empty the table the odds matcher depends on.
            rows = client.teams(season)
            if not rows:
                return 0
            return replace_rows(conn, "teams", rows, where="1 = 1")

        if dataset == "games":
            rows = client.games(season)
            return replace_rows(
                conn, "games", rows, where="season = ?", params=[season]
            )

        if dataset == "drives":
            rows = client.drives(season)
            return replace_rows(
                conn, "drives", rows, where="season = ?", params=[season]
            )

        if dataset == "plays":
            if skip_settled_plays and _season_is_settled(conn, season):
                log.info("Season %s plays already complete; skipping", season)
                return 0
            rows = client.plays(season)
            return replace_rows(
                conn, "plays", rows, where="season = ?", params=[season]
            )

        if dataset == "lines":
            rows = client.lines(season)
            return replace_rows(
                conn,
                "lines",
                rows,
                where="game_id IN (SELECT game_id FROM games WHERE season = ?)",
                params=[season],
            )

        if dataset == "talent":
            rows = client.talent(season)
            return replace_rows(
                conn, "talent", rows, where="season = ?", params=[season]
            )

        if dataset == "recruiting":
            rows = client.recruiting(season)
            return replace_rows(
                conn, "recruiting", rows, where="season = ?", params=[season]
            )

        if dataset == "portal":
            rows = client.portal(season)
            return replace_rows(
                conn, "portal", rows, where="season = ?", params=[season]
            )

    raise ValueError(f"Unknown dataset {dataset!r}")


def _season_is_settled(conn, season: int) -> bool:
    """True when a season's plays are loaded and none of its games can change.

    Deliberately conservative: the current season is never settled even if
    every game it has scheduled happens to be marked complete, because more
    games are coming.
    """
    if season >= current_season():
        return False
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM plays WHERE season = ?) AS play_count,
            (SELECT count(*) FROM games WHERE season = ? AND NOT completed) AS pending
        """,
        [season, season],
    ).fetchone()
    return bool(row and row[0] > 0 and row[1] == 0)


def refresh_live_odds() -> int:
    """Poll the books for current prices. Returns rows appended.

    Append-only: every poll is kept so the UI can show line movement and so a
    flagged edge can later be scored against the closing number.
    """
    cfg = settings()
    if not cfg.has_odds_api:
        log.info("No Odds API key configured; skipping live odds")
        return 0

    with cursor() as conn:
        teams = conn.execute("SELECT school, mascot FROM teams").fetchall()
    if not teams:
        log.warning("No teams loaded; run an ingest before polling live odds")
        return 0

    matcher = TeamMatcher([(row[0], row[1]) for row in teams])
    with OddsAPIClient() as client:
        rows = client.odds(matcher)
        log.info(
            "Fetched %d live odds rows (quota remaining: %s)",
            len(rows),
            client.quota.remaining,
        )

    if not rows:
        return 0
    with cursor() as conn:
        return replace_rows(conn, "live_odds", rows)


def latest_live_odds(conn, market: str = "spreads"):
    """The most recent poll's prices for each game and book.

    ``live_odds`` is append-only, so "current" means "from the latest
    fetched_at for that event and book" — not the latest overall, since a book
    that drops a game keeps its older rows and would otherwise vanish.
    """
    return conn.execute(
        """
        WITH ranked AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY event_id, book, market, outcome
                       ORDER BY fetched_at DESC
                   ) AS recency
            FROM live_odds
            WHERE market = ?
        )
        SELECT event_id, commence_time, home_team, away_team, book,
               market, outcome, price, point, fetched_at
        FROM ranked
        WHERE recency = 1
        ORDER BY commence_time, home_team, book
        """,
        [market],
    ).df()
