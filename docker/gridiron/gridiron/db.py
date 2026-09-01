"""The DuckDB store: connection handling and schema.

WHY DUCKDB AND NOT POSTGRES. Everything this service does is an analytical
scan — "group ten million plays by team and season, weighted by recency". A
columnar embedded engine does that in the time a round trip to Postgres would
take, with no second pod, no second PVC, and no backup story beyond copying one
file.

The cost is that DuckDB takes a single writer. That shapes the deployment: one
process owns the database file, ingest runs inside it on a schedule rather than
as a separate CronJob, and the PVC is ReadWriteOnce local-path rather than NFS
(DuckDB's file locking over NFS is not something to bet a dataset on). If this
ever needs concurrent writers it wants Postgres and a rewrite of this module —
nothing above ``db`` knows which engine it is talking to.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

from .config import settings

# DuckDB connections are not safe to share across threads, and the API server
# is threaded. Rather than a pool — which would reintroduce the multi-writer
# problem — every caller takes this lock for the duration of its work. The
# workload is one analyst's queries, so contention is not a real cost.
_LOCK = threading.RLock()
_CONN: duckdb.DuckDBPyConnection | None = None


SCHEMA = """
-- ---------------------------------------------------------------------------
-- Source tables. These mirror what the upstream APIs return, lightly
-- normalised. Nothing here is derived, so a schema change to the feature layer
-- never costs a re-download.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS teams (
    school          TEXT PRIMARY KEY,
    mascot          TEXT,
    conference      TEXT,
    classification  TEXT
);

CREATE TABLE IF NOT EXISTS games (
    game_id             BIGINT PRIMARY KEY,
    season              INTEGER NOT NULL,
    week                INTEGER,
    season_type         TEXT,
    start_date          TIMESTAMP,
    neutral_site        BOOLEAN,
    conference_game     BOOLEAN,
    completed           BOOLEAN,
    home_team           TEXT,
    away_team           TEXT,
    home_conference     TEXT,
    away_conference     TEXT,
    home_classification TEXT,
    away_classification TEXT,
    home_points         INTEGER,
    away_points         INTEGER,
    venue               TEXT
);

CREATE TABLE IF NOT EXISTS drives (
    drive_id             TEXT PRIMARY KEY,
    game_id              BIGINT,
    season               INTEGER,
    offense              TEXT,
    defense              TEXT,
    drive_number         INTEGER,
    start_period         INTEGER,
    -- Distance to the opponent's goal line at the snap that started the drive:
    -- 75 means the offense took over on its own 25. Every field-position
    -- feature in this package is built on this one column.
    start_yards_to_goal  INTEGER,
    end_yards_to_goal    INTEGER,
    plays                INTEGER,
    yards                INTEGER,
    drive_result         TEXT,
    scoring              BOOLEAN,
    -- Scoreboard either side of the drive. The points a drive was worth are
    -- read from these rather than parsed out of `drive_result`, which is a
    -- free-text label with a long tail ("PUNT RETURN TD", "FUMBLE RETURN TD",
    -- "MISSED FG TD") whose sign is easy to get backwards. A subtraction
    -- cannot be got backwards, and it prices extra points and two-point
    -- conversions correctly for free.
    start_offense_score  INTEGER,
    start_defense_score  INTEGER,
    end_offense_score    INTEGER,
    end_defense_score    INTEGER
);

CREATE TABLE IF NOT EXISTS plays (
    play_id         TEXT PRIMARY KEY,
    game_id         BIGINT,
    drive_id        TEXT,
    season          INTEGER,
    offense         TEXT,
    defense         TEXT,
    period          INTEGER,
    down            INTEGER,
    distance        INTEGER,
    yards_to_goal   INTEGER,
    yards_gained    INTEGER,
    play_type       TEXT,
    scoring         BOOLEAN,
    ppa             DOUBLE
);

-- Historical closing lines, one row per game per book. The `spread` column is
-- the book's own convention (favorite negative, home perspective); code that
-- reasons about direction converts to a home margin first. See the package
-- docstring.
CREATE TABLE IF NOT EXISTS lines (
    game_id          BIGINT,
    provider         TEXT,
    spread           DOUBLE,
    spread_open      DOUBLE,
    over_under       DOUBLE,
    over_under_open  DOUBLE,
    home_moneyline   INTEGER,
    away_moneyline   INTEGER,
    PRIMARY KEY (game_id, provider)
);

CREATE TABLE IF NOT EXISTS talent (
    season  INTEGER,
    school  TEXT,
    talent  DOUBLE,
    PRIMARY KEY (season, school)
);

CREATE TABLE IF NOT EXISTS recruiting (
    season  INTEGER,
    school  TEXT,
    rank    INTEGER,
    points  DOUBLE,
    PRIMARY KEY (season, school)
);

-- Transfer portal movements. This is the empirical handle on the NIL/portal
-- thesis: talent leaving blue-blood benches for starting jobs elsewhere is a
-- row in this table, not a vibe.
CREATE TABLE IF NOT EXISTS portal (
    season        INTEGER,
    first_name    TEXT,
    last_name     TEXT,
    position      TEXT,
    origin        TEXT,
    destination   TEXT,
    transfer_date TIMESTAMP,
    rating        DOUBLE,
    stars         INTEGER,
    eligibility   TEXT
);

-- Live prices, append-only. Keeping every poll rather than the latest lets the
-- UI show line movement and lets closing-line value be measured against what
-- was actually available when a bet was flagged.
CREATE TABLE IF NOT EXISTS live_odds (
    fetched_at     TIMESTAMP,
    event_id       TEXT,
    commence_time  TIMESTAMP,
    home_team      TEXT,      -- normalised to the CFBD school name
    away_team      TEXT,
    book           TEXT,
    market         TEXT,      -- spreads | h2h | totals
    outcome        TEXT,      -- team name, or Over/Under
    price          INTEGER,   -- American odds
    point          DOUBLE     -- the handicap or total; NULL for moneylines
);

-- ---------------------------------------------------------------------------
-- Derived tables. Everything below is rebuildable from the tables above by
-- `gridiron features build`, so dropping them is always safe.
-- ---------------------------------------------------------------------------

-- One row per team per game — the join point for every feature and the only
-- table the models read. Two rows per game, one from each side's perspective.
CREATE TABLE IF NOT EXISTS team_game (
    game_id                 BIGINT,
    season                  INTEGER,
    week                    INTEGER,
    kickoff                 TIMESTAMP,
    team                    TEXT,
    opponent                TEXT,
    is_home                 BOOLEAN,
    neutral_site            BOOLEAN,
    points                  INTEGER,
    points_allowed          INTEGER,

    -- Hidden yardage: field position -----------------------------------------
    drives                  INTEGER,
    avg_start_yards_to_goal DOUBLE,   -- own drives; lower is better field position
    def_start_yards_to_goal DOUBLE,   -- opponent's drives; higher is better
    fp_points               DOUBLE,   -- expected points handed to this offense by its start spots
    def_fp_points           DOUBLE,   -- expected points handed to the opponent
    fp_margin_pts           DOUBLE,   -- fp_points - def_fp_points, per game

    -- Hidden yardage: negative-play salvage -----------------------------------
    rushes                  INTEGER,
    stuff_rate              DOUBLE,   -- share of rushes gaining <= 0
    avg_stuff_yards         DOUBLE,   -- mean yards on those rushes (negative)
    salvage_yards_per_rush  DOUBLE,   -- yards/rush saved vs the league's stuffed-rush loss
    sacks_taken             INTEGER,
    avg_sack_yards          DOUBLE,

    -- Efficiency --------------------------------------------------------------
    plays                   INTEGER,
    success_rate            DOUBLE,
    explosiveness           DOUBLE,   -- mean PPA on successful plays
    yards_per_play          DOUBLE,
    def_success_rate        DOUBLE,
    def_yards_per_play      DOUBLE,

    -- Margin decomposition (the model's actual inputs) ------------------------
    margin                  DOUBLE,   -- points - points_allowed
    turnover_margin         DOUBLE,
    turnover_luck_pts       DOUBLE,   -- points attributable to turnover margin
    efficiency_margin       DOUBLE,   -- margin with field position and turnover luck removed

    PRIMARY KEY (game_id, team)
);

-- The fitted expected-points-by-field-position curve, stored so the UI can
-- draw it and so a backtest can prove which vintage it used.
CREATE TABLE IF NOT EXISTS fp_curve (
    fit_key             TEXT,      -- 'global', or an as_of stamp for a backtest vintage
    yards_to_goal_bin   INTEGER,
    expected_points     DOUBLE,
    drives              BIGINT,
    PRIMARY KEY (fit_key, yards_to_goal_bin)
);

-- ---------------------------------------------------------------------------
-- Research output. Backtests are persisted so a theory can be compared with
-- one you ran a month ago instead of re-run from memory.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TIMESTAMP,
    label        TEXT,
    model        TEXT,
    params       JSON,
    first_season INTEGER,
    last_season  INTEGER,
    metrics      JSON
);

CREATE TABLE IF NOT EXISTS backtest_bets (
    run_id         TEXT,
    game_id        BIGINT,
    season         INTEGER,
    week           INTEGER,
    kickoff        TIMESTAMP,
    home_team      TEXT,
    away_team      TEXT,
    model_margin   DOUBLE,
    market_margin  DOUBLE,
    actual_margin  DOUBLE,
    edge           DOUBLE,
    side           TEXT,     -- 'home' | 'away' | 'pass'
    result         TEXT,     -- 'win' | 'loss' | 'push' | 'pass'
    profit_units   DOUBLE
);

CREATE INDEX IF NOT EXISTS backtest_bets_run ON backtest_bets (run_id);
CREATE INDEX IF NOT EXISTS plays_game ON plays (game_id);
CREATE INDEX IF NOT EXISTS drives_game ON drives (game_id);
CREATE INDEX IF NOT EXISTS games_season ON games (season, week);
CREATE INDEX IF NOT EXISTS team_game_team ON team_game (team, kickoff);
"""


def _open(path: Path | None = None) -> duckdb.DuckDBPyConnection:
    path = path or settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute("SET TimeZone = 'UTC'")
    return conn


def connection() -> duckdb.DuckDBPyConnection:
    """The process-wide connection, opened and migrated on first use."""
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = _open()
            init_schema(_CONN)
        return _CONN


@contextmanager
def cursor() -> Iterator[duckdb.DuckDBPyConnection]:
    """Serialise access to the single connection.

    Held for the whole body, so a read-modify-write in a caller is atomic
    against other threads.
    """
    conn = connection()
    with _LOCK:
        yield conn


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create every table and index. Safe to run against a populated store."""
    conn.execute(SCHEMA)


def reset_connection() -> None:
    """Close the cached connection. Used by tests and by `serve` on shutdown."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None


def replace_rows(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    rows: list[dict],
    *,
    where: str | None = None,
    params: list | None = None,
) -> int:
    """Delete-then-insert a batch, so re-ingesting a season is idempotent.

    Upserting on the primary key would leave rows behind when the upstream
    source drops a record — a game that gets rescheduled to a different id, a
    play that is corrected away. Replacing the whole partition matches how the
    sources actually behave.
    """
    if where is not None:
        conn.execute(f"DELETE FROM {table} WHERE {where}", params or [])
    if not rows:
        return 0

    import pandas as pd

    frame = pd.DataFrame(rows)
    # Insert by name rather than position: the frame's column order follows
    # whatever the source dict happened to iterate, which is not the table's.
    columns = ", ".join(f'"{c}"' for c in frame.columns)
    conn.register("_incoming", frame)
    try:
        conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _incoming")
    finally:
        conn.unregister("_incoming")
    return len(frame)


def table_counts() -> dict[str, int]:
    """Row counts for every table, for the status page and `gridiron status`."""
    with cursor() as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        return {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in names
        }
