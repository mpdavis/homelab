"""The feature blocks that ship, and the builder that assembles ``team_game``.

The interesting one is :func:`field_position`. Everything else is standard
efficiency bookkeeping that exists so a hidden-yardage claim can be tested
*against* something rather than admired on its own.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import block_columns, feature_block, registered_blocks

log = logging.getLogger(__name__)

# Field position is bucketed rather than fitted as a smooth curve. Five yards
# is fine enough that the within-bucket variation is small and coarse enough
# that even a lightly-played bucket has thousands of drives behind it.
FP_BIN_WIDTH = 5

# A turnover is worth roughly four to five points of scoring margin in college
# football, most of it the field position it hands over. This constant is only
# used for the informational `turnover_luck_pts` column — the margin
# decomposition subtracts field position, which already contains the part of a
# turnover's cost that shows up as a short field. Subtracting both would
# double-count. See `efficiency_margin` below.
POINTS_PER_TURNOVER = 4.0

# Points a drive was worth to its offense. Read from the scoreboard when CFBD
# supplies it, which prices PATs, two-point tries and defensive returns for
# free; the CASE arms are a fallback for rows where the score columns are null.
DRIVE_POINTS_SQL = """
CASE
    WHEN d.end_offense_score IS NOT NULL AND d.start_offense_score IS NOT NULL
        THEN (d.end_offense_score - d.start_offense_score)
             - (coalesce(d.end_defense_score, 0) - coalesce(d.start_defense_score, 0))
    WHEN upper(coalesce(d.drive_result, '')) LIKE '%RETURN TD%' THEN -7
    WHEN upper(coalesce(d.drive_result, '')) IN ('INT TD', 'FUMBLE TD') THEN -7
    WHEN upper(coalesce(d.drive_result, '')) LIKE '%SAFETY%' THEN -2
    WHEN upper(coalesce(d.drive_result, '')) LIKE '%TD%' THEN 7
    WHEN upper(coalesce(d.drive_result, '')) LIKE 'FG%'
         AND upper(coalesce(d.drive_result, '')) NOT LIKE '%MISS%' THEN 3
    ELSE 0
END
"""

# Kneel-downs at the end of a half start from wherever the previous play left
# off and are worth nothing by construction. Leaving them in drags the
# expected-points curve down hardest exactly where good field position lives.
REAL_DRIVE_FILTER = """
    d.start_yards_to_goal IS NOT NULL
    AND d.start_yards_to_goal BETWEEN 1 AND 99
    AND upper(coalesce(d.drive_result, '')) NOT LIKE '%END OF%'
"""

# Play classification. CFBD's play_type is free text with a long tail, so these
# are pattern matches rather than an enumeration — a new label for a trick play
# should land in a sensible bucket rather than vanish.
PLAY_FLAGS_SQL = f"""
    p.play_type IN ('Rush', 'Rushing Touchdown') AS is_rush,
    (p.play_type ILIKE '%Pass%' OR p.play_type ILIKE '%Sack%'
     OR p.play_type ILIKE '%Interception%') AS is_pass,
    p.play_type ILIKE '%Sack%' AS is_sack,
    (p.play_type ILIKE '%Interception%'
     OR p.play_type ILIKE '%Fumble Recovery (Opponent)%'
     OR p.play_type ILIKE '%Fumble Return%') AS is_turnover,
    CASE
        WHEN p.distance IS NULL OR p.distance <= 0 THEN NULL
        WHEN p.down = 1 THEN p.yards_gained >= 0.5 * p.distance
        WHEN p.down = 2 THEN p.yards_gained >= 0.7 * p.distance
        WHEN p.down IN (3, 4) THEN p.yards_gained >= p.distance
        ELSE NULL
    END AS is_success
"""


def _season_filter(seasons: list[int] | None, alias: str) -> tuple[str, list]:
    if not seasons:
        return "", []
    placeholders = ", ".join("?" for _ in seasons)
    return f" AND {alias}.season IN ({placeholders})", list(seasons)


# ---------------------------------------------------------------------------
# Expected points by field position — the ruler everything hidden is measured
# against.
# ---------------------------------------------------------------------------


def fit_fp_curve(conn, *, fit_key: str = "global", as_of=None) -> pd.DataFrame:
    """Fit and store expected drive points as a function of starting position.

    This is the conversion that turns "we start our drives on the 32 and they
    start on the 21" into a number of points per game, which is the only form
    in which field position can be compared with anything else.

    ON LEAKAGE. The curve is close to a structural constant of the sport — the
    value of a drive from your own 25 has moved by a fraction of a point in a
    decade — so the default ``fit_key='global'`` fits it on everything and
    reuses it. That is a deliberate, documented, very small lookahead. Pass
    ``as_of`` to fit a strictly point-in-time vintage instead; the backtester
    does this when ``strict_fp_curve`` is set.
    """
    params: list = []
    cutoff = ""
    if as_of is not None:
        cutoff = " AND g.start_date < ?"
        params.append(as_of)

    frame = conn.execute(
        f"""
        SELECT
            CAST(FLOOR(d.start_yards_to_goal / {FP_BIN_WIDTH}.0) * {FP_BIN_WIDTH}
                 AS INTEGER)      AS yards_to_goal_bin,
            avg({DRIVE_POINTS_SQL}) AS expected_points,
            count(*)                AS drives
        FROM drives d
        JOIN games g ON g.game_id = d.game_id
        WHERE g.completed AND {REAL_DRIVE_FILTER}{cutoff}
        GROUP BY 1
        ORDER BY 1
        """,
        params,
    ).df()

    if frame.empty:
        raise ValueError(
            "No drives available to fit the field-position curve — ingest "
            "drives before building features."
        )

    # Thinly-populated buckets at the extremes (a drive starting on the
    # opponent's 1) are noisy. Smoothing across neighbours keeps the curve
    # monotone-ish without pretending to more precision than the data has.
    frame = frame.sort_values("yards_to_goal_bin").reset_index(drop=True)
    frame["expected_points"] = (
        frame["expected_points"].rolling(window=3, center=True, min_periods=1).mean()
    )
    frame.insert(0, "fit_key", fit_key)

    conn.execute("DELETE FROM fp_curve WHERE fit_key = ?", [fit_key])
    conn.register("_curve", frame)
    try:
        conn.execute(
            "INSERT INTO fp_curve (fit_key, yards_to_goal_bin, expected_points, drives) "
            "SELECT fit_key, yards_to_goal_bin, expected_points, drives FROM _curve"
        )
    finally:
        conn.unregister("_curve")
    log.info("Fitted field-position curve %r over %d buckets", fit_key, len(frame))
    return frame


# ---------------------------------------------------------------------------
# Feature blocks
# ---------------------------------------------------------------------------


@feature_block(
    "field_position",
    columns=[
        "drives",
        "avg_start_yards_to_goal",
        "def_start_yards_to_goal",
        "fp_points",
        "def_fp_points",
        "fp_margin_pts",
    ],
    description="Hidden yardage: what each side's starting spots were worth in points",
)
def field_position(conn, seasons=None, fit_key: str = "global") -> pd.DataFrame:
    """Points handed to each offence by where its drives began.

    ``fp_margin_pts`` is the headline: the expected points this team's starting
    field position was worth minus the expected points it gave the opponent, in
    one game. A team at +4 was gifted most of a touchdown before running a
    play, and none of it appears in yards, yards per play or any box score.

    A CAVEAT WORTH KEEPING IN VIEW. Field position is partly *earned* — score a
    touchdown and the opponent starts at their 25; go three-and-out and yours
    is short. So this is not a pure special-teams number, and it is correlated
    with the offence it is supposed to be separate from. What makes it useful
    anyway is that the correlation is far from one: the residual, after
    efficiency is accounted for, is real and is mostly punting, coverage,
    returns and turnover position. The decomposed model exploits exactly that
    residual, and it rates it with its own half-life because it regresses to
    the mean faster than efficiency does.
    """
    where, params = _season_filter(seasons, "d")
    bin_expr = (
        f"CAST(FLOOR(d.start_yards_to_goal / {FP_BIN_WIDTH}.0) * {FP_BIN_WIDTH} AS INTEGER)"
    )

    offense = conn.execute(
        f"""
        SELECT d.game_id, d.offense AS team,
               count(*)                      AS drives,
               avg(d.start_yards_to_goal)    AS avg_start_yards_to_goal,
               sum(c.expected_points)        AS fp_points
        FROM drives d
        JOIN games g ON g.game_id = d.game_id
        JOIN fp_curve c
          ON c.fit_key = ? AND c.yards_to_goal_bin = {bin_expr}
        WHERE g.completed AND {REAL_DRIVE_FILTER}{where}
        GROUP BY 1, 2
        """,
        [fit_key, *params],
    ).df()

    defense = conn.execute(
        f"""
        SELECT d.game_id, d.defense AS team,
               avg(d.start_yards_to_goal) AS def_start_yards_to_goal,
               sum(c.expected_points)     AS def_fp_points
        FROM drives d
        JOIN games g ON g.game_id = d.game_id
        JOIN fp_curve c
          ON c.fit_key = ? AND c.yards_to_goal_bin = {bin_expr}
        WHERE g.completed AND {REAL_DRIVE_FILTER}{where}
        GROUP BY 1, 2
        """,
        [fit_key, *params],
    ).df()

    frame = offense.merge(defense, on=["game_id", "team"], how="outer")
    frame["fp_margin_pts"] = frame["fp_points"] - frame["def_fp_points"]
    return frame


@feature_block(
    "negative_plays",
    columns=[
        "rushes",
        "stuff_rate",
        "avg_stuff_yards",
        "salvage_yards_per_rush",
        "sacks_taken",
        "avg_sack_yards",
    ],
    description="Hidden yardage: turning an eight-yard loss into a one-yard loss",
)
def negative_plays(conn, seasons=None, **_) -> pd.DataFrame:
    """How much a team loses when a play is already blown up.

    Two teams can have identical stuff rates and very different offences. One
    of them has a back who gets tackled at the point of contact for -7; the
    other has a back who breaks the first hit and falls forward for -1. Six
    yards a snap, several snaps a game, and neither yards-per-carry nor success
    rate distinguishes them cleanly — success rate calls both a failure, and
    yards-per-carry buries the difference in the average.

    ``salvage_yards_per_rush`` isolates it: stuff rate times how much less
    ground this team gives up on a stuffed run than the league does that
    season. Positive is good. Multiply by carries for yards saved per game.
    """
    where, params = _season_filter(seasons, "p")
    frame = conn.execute(
        f"""
        WITH flagged AS (
            SELECT p.game_id, p.offense AS team, p.season, p.yards_gained,
                   {PLAY_FLAGS_SQL}
            FROM plays p
            WHERE p.offense IS NOT NULL{where}
        ),
        league AS (
            SELECT season, avg(yards_gained) AS league_stuff_yards
            FROM flagged
            WHERE is_rush AND yards_gained <= 0
            GROUP BY season
        ),
        per_game AS (
            SELECT game_id, team, season,
                   count(*) FILTER (WHERE is_rush)                     AS rushes,
                   avg(CASE WHEN yards_gained <= 0 THEN 1.0 ELSE 0.0 END)
                       FILTER (WHERE is_rush)                          AS stuff_rate,
                   avg(yards_gained)
                       FILTER (WHERE is_rush AND yards_gained <= 0)    AS avg_stuff_yards,
                   count(*) FILTER (WHERE is_sack)                     AS sacks_taken,
                   avg(yards_gained) FILTER (WHERE is_sack)            AS avg_sack_yards
            FROM flagged
            GROUP BY 1, 2, 3
        )
        SELECT pg.game_id, pg.team, pg.rushes, pg.stuff_rate, pg.avg_stuff_yards,
               pg.stuff_rate * (pg.avg_stuff_yards - l.league_stuff_yards)
                   AS salvage_yards_per_rush,
               pg.sacks_taken, pg.avg_sack_yards
        FROM per_game pg
        LEFT JOIN league l USING (season)
        """,
        params,
    ).df()
    # A team with no stuffed runs in a game salvaged nothing, which is zero
    # rather than unknown — leaving it null would drop the game from any model
    # that uses the column.
    frame["salvage_yards_per_rush"] = pd.to_numeric(
        frame["salvage_yards_per_rush"], errors="coerce"
    ).fillna(0.0)
    return frame


@feature_block(
    "efficiency",
    columns=[
        "plays",
        "success_rate",
        "explosiveness",
        "yards_per_play",
        "def_success_rate",
        "def_yards_per_play",
        "turnover_margin",
    ],
    description="Down-to-down efficiency on both sides of the ball",
)
def efficiency(conn, seasons=None, **_) -> pd.DataFrame:
    """Success rate, explosiveness and turnover margin.

    The conventional signal. It is here so that a hidden-yardage claim has to
    beat it rather than merely exist, and so the decomposed model has something
    to rate once field position has been taken out of the margin.
    """
    where, params = _season_filter(seasons, "p")
    offense = conn.execute(
        f"""
        WITH flagged AS (
            SELECT p.game_id, p.offense AS team, p.defense AS opponent,
                   p.yards_gained, p.ppa, {PLAY_FLAGS_SQL}
            FROM plays p
            WHERE p.offense IS NOT NULL{where}
        )
        SELECT game_id, team,
               count(*) FILTER (WHERE is_rush OR is_pass)             AS plays,
               avg(CASE WHEN is_success THEN 1.0 ELSE 0.0 END)
                   FILTER (WHERE (is_rush OR is_pass) AND is_success IS NOT NULL)
                                                                      AS success_rate,
               avg(ppa) FILTER (WHERE (is_rush OR is_pass) AND is_success)
                                                                      AS explosiveness,
               avg(yards_gained::DOUBLE) FILTER (WHERE is_rush OR is_pass)
                                                                      AS yards_per_play,
               count(*) FILTER (WHERE is_turnover)                    AS giveaways
        FROM flagged
        GROUP BY 1, 2
        """,
        params,
    ).df()

    defense = conn.execute(
        f"""
        WITH flagged AS (
            SELECT p.game_id, p.defense AS team, p.yards_gained, {PLAY_FLAGS_SQL}
            FROM plays p
            WHERE p.defense IS NOT NULL{where}
        )
        SELECT game_id, team,
               avg(CASE WHEN is_success THEN 1.0 ELSE 0.0 END)
                   FILTER (WHERE (is_rush OR is_pass) AND is_success IS NOT NULL)
                                                                   AS def_success_rate,
               avg(yards_gained::DOUBLE) FILTER (WHERE is_rush OR is_pass)
                                                                   AS def_yards_per_play,
               count(*) FILTER (WHERE is_turnover)                 AS takeaways
        FROM flagged
        GROUP BY 1, 2
        """,
        params,
    ).df()

    frame = offense.merge(defense, on=["game_id", "team"], how="outer")
    # An outer join leaves nulls on either side; a team that forced no
    # turnovers forced zero of them, which is a number rather than an unknown.
    frame["turnover_margin"] = pd.to_numeric(
        frame["takeaways"], errors="coerce"
    ).fillna(0.0) - pd.to_numeric(frame["giveaways"], errors="coerce").fillna(0.0)
    return frame.drop(columns=["takeaways", "giveaways"])


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_team_game(
    conn,
    seasons: list[int] | None = None,
    *,
    fit_key: str = "global",
    refit_curve: bool = True,
) -> int:
    """Rebuild ``team_game`` for the given seasons. Returns rows written.

    Cheap enough to run after every ingest — a decade is a few seconds — so it
    is not incremental. That is worth more than the seconds it costs: a feature
    definition can be changed and the whole history is consistent with it
    immediately, with no partially-migrated rows to reason about.
    """
    if refit_curve:
        existing = conn.execute(
            "SELECT count(*) FROM fp_curve WHERE fit_key = ?", [fit_key]
        ).fetchone()[0]
        if not existing:
            fit_fp_curve(conn, fit_key=fit_key)

    where, params = _season_filter(seasons, "g")
    sides = conn.execute(
        f"""
        SELECT g.game_id, g.season, g.week, g.start_date AS kickoff,
               g.home_team AS team, g.away_team AS opponent,
               TRUE AS is_home, g.neutral_site,
               g.home_points AS points, g.away_points AS points_allowed
        FROM games g
        WHERE g.completed AND g.home_points IS NOT NULL
              AND g.away_points IS NOT NULL{where}
        UNION ALL
        SELECT g.game_id, g.season, g.week, g.start_date,
               g.away_team, g.home_team,
               FALSE, g.neutral_site,
               g.away_points, g.home_points
        FROM games g
        WHERE g.completed AND g.home_points IS NOT NULL
              AND g.away_points IS NOT NULL{where}
        """,
        params + params,
    ).df()

    if sides.empty:
        log.warning("No completed games for seasons %s; team_game left alone", seasons)
        return 0

    for name, block in registered_blocks().items():
        try:
            contribution = block.fn(conn, seasons, fit_key=fit_key)
        except TypeError:
            contribution = block.fn(conn, seasons)
        if contribution is None or contribution.empty:
            log.warning("Feature block %r produced no rows", name)
            continue
        overlap = (set(contribution.columns) & set(sides.columns)) - {"game_id", "team"}
        if overlap:
            raise ValueError(
                f"Feature block {name!r} would overwrite existing columns: "
                f"{sorted(overlap)}"
            )
        sides = sides.merge(contribution, on=["game_id", "team"], how="left")

    # A block that produced nothing leaves its columns absent entirely, and
    # the margin decomposition below reads some of them by name. Filling them
    # in as null here means a database with games but no play-by-play — a
    # partial ingest, or a season CFBD has not published drives for — builds a
    # usable `team_game` with empty hidden-yardage columns instead of raising.
    for column in block_columns():
        if column not in sides.columns:
            sides[column] = np.nan

    sides["margin"] = sides["points"] - sides["points_allowed"]
    sides["turnover_luck_pts"] = (
        pd.to_numeric(
            sides.get("turnover_margin", pd.Series(0.0, index=sides.index)),
            errors="coerce",
        ).fillna(0.0)
        * POINTS_PER_TURNOVER
    )
    # Scoring margin with the field-position gift removed. This is what the
    # decomposed model rates as "how good were they, actually" — the part of
    # the result that came from moving the ball rather than from where it was
    # handed to them. Turnover luck is NOT subtracted here: its main effect is
    # the short field it produces, which fp_margin_pts has already counted.
    sides["efficiency_margin"] = sides["margin"] - pd.to_numeric(
        sides["fp_margin_pts"], errors="coerce"
    ).fillna(0.0)

    columns = [
        "game_id", "season", "week", "kickoff", "team", "opponent", "is_home",
        "neutral_site", "points", "points_allowed",
        "drives", "avg_start_yards_to_goal", "def_start_yards_to_goal",
        "fp_points", "def_fp_points", "fp_margin_pts",
        "rushes", "stuff_rate", "avg_stuff_yards", "salvage_yards_per_rush",
        "sacks_taken", "avg_sack_yards",
        "plays", "success_rate", "explosiveness", "yards_per_play",
        "def_success_rate", "def_yards_per_play",
        "margin", "turnover_margin", "turnover_luck_pts", "efficiency_margin",
    ]
    for column in columns:
        if column not in sides.columns:
            sides[column] = np.nan
    frame = sides[columns]

    if seasons:
        placeholders = ", ".join("?" for _ in seasons)
        conn.execute(f"DELETE FROM team_game WHERE season IN ({placeholders})", seasons)
    else:
        conn.execute("DELETE FROM team_game")

    conn.register("_team_game", frame)
    try:
        conn.execute(
            f"INSERT INTO team_game ({', '.join(columns)}) "
            f"SELECT {', '.join(columns)} FROM _team_game"
        )
    finally:
        conn.unregister("_team_game")
    log.info("Built team_game: %d rows", len(frame))
    return len(frame)
