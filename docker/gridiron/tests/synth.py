"""A small deterministic universe, used by every test that needs a database.

Real fixtures would mean either a network call or a vendored dump of someone
else's data. This instead generates seasons of football from known parameters,
which has the property that matters for testing a rating system: the right
answer is known in advance, so a fit can be checked against it rather than
against itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# The truth the tests get to check against. Units are points of scoring margin
# against an average opponent.
TRUE_RATINGS = {
    "Alpha": 14.0,
    "Bravo": 9.0,
    "Charlie": 4.0,
    "Delta": 1.0,
    "Echo": -2.0,
    "Foxtrot": -6.0,
    "Golf": -9.0,
    "Hotel": -13.0,
}
TRUE_HFA = 2.5
TEAMS = list(TRUE_RATINGS)
SEASONS = (2021, 2022, 2023)
# The analysis module refuses to report on thin samples — 200 games for the
# brand-premium regression, 30 team-seasons for split-half reliability. Those
# floors are correct for real data, so testing those code paths needs a
# universe big enough to clear them rather than a lower floor.
LONG_SEASONS = (2018, 2019, 2020, 2021, 2022, 2023)
WEEKS = range(1, 13)

# How much the market overpays for brand, in points per standard deviation of
# prestige. Planted with a sign so `brand_premium` has a known answer to find:
# negative means prestigious teams fall short of their number.
PLANTED_BRAND_PREMIUM = -1.5

# Prestige, for the blue-blood machinery. Recruiting points feed the EWMA that
# `team_season_prestige` z-scores, so these only need to be ordered.
RECRUITING_POINTS = {
    "Alpha": 290.0,
    "Bravo": 270.0,
    "Charlie": 250.0,
    "Delta": 235.0,
    "Echo": 225.0,
    "Foxtrot": 215.0,
    "Golf": 205.0,
    "Hotel": 190.0,
}

# Teams at the ends of this list are deliberately good and bad at salvaging a
# blown-up run, so `salvage_yards_per_rush` has a known sign to check.
SALVAGE_SKILL = {
    team: 2.0 - 4.0 * i / (len(TEAMS) - 1) for i, team in enumerate(TEAMS)
}


# The provider mix CFBD actually returns, which changes partway through the
# decade: a merged `consensus` row exists through 2022 and vanishes after, and
# the individual books rotate. Reproducing that here is the point — it is the
# shape that silently emptied the NIL-era seasons out of every analysis.
CONSENSUS_LAST_SEASON = 2022
MODERN_BOOKS = ("ESPN Bet", "DraftKings", "Bovada")
LEGACY_BOOKS = ("William Hill (New Jersey)", "Bovada")


def _lines(rng, game_id: int, season: int, priced: float) -> list[dict]:
    """One row per book, priced around `priced` with a little book-to-book spread."""
    if season <= CONSENSUS_LAST_SEASON:
        providers = ("consensus",) + LEGACY_BOOKS
    else:
        providers = MODERN_BOOKS
    out = []
    for provider in providers:
        # Books disagree by a fraction of a point; the median across them is
        # what `line_series` reconstructs.
        jitter = 0.0 if provider == "consensus" else float(rng.normal(0, 0.35))
        out.append(
            {
                "game_id": game_id,
                "provider": provider,
                "spread": -round((priced + jitter) * 2) / 2,
                "spread_open": -round((priced + jitter + rng.normal(0, 0.6)) * 2) / 2,
                "over_under": 52.5,
                "over_under_open": 52.5,
                "home_moneyline": -200,
                "away_moneyline": 170,
            }
        )
    return out


def _scoreline(margin: float, rng) -> tuple[int, int]:
    """A plausible scoreline with *exactly* this margin.

    Sampling a score and clipping it at zero would be the obvious way to do
    this, and it is wrong: clipping compresses lopsided results, and how
    lopsided a game is correlates with everything these tests are trying to
    measure. The bias it introduces looks exactly like a real effect.
    """
    points = int(round(margin))
    base = int(np.clip(round(rng.normal(21, 6)), 7, 45))
    return base + max(points, 0), base - min(points, 0)


def prestige_z() -> dict[str, float]:
    """The prestige index the package will compute, worked out independently.

    ``team_season_prestige`` decays recruiting classes and z-scores them within
    a season. Recruiting is constant per team here, so the decay is a no-op and
    the index reduces to a plain z-score — which lets a test plant an effect of
    a known size against a known regressor.
    """
    points = np.array([RECRUITING_POINTS[team] for team in TEAMS], dtype=float)
    z = (points - points.mean()) / points.std(ddof=0)
    return dict(zip(TEAMS, z))


def _kickoff(season: int, week: int, slot: int) -> datetime:
    """Weekly kickoffs, distinct within a week so ordering is total."""
    return datetime(season, 8, 28) + timedelta(days=7 * (week - 1), hours=12 + slot)


def _pairings(week: int) -> list[tuple[str, str]]:
    """A circle-method round robin, so every team plays exactly once a week."""
    spin = (week - 1) % (len(TEAMS) - 1)
    rotation = [TEAMS[0]] + TEAMS[1:][spin:] + TEAMS[1:][:spin]
    half = len(rotation) // 2
    left, right = rotation[:half], rotation[half:][::-1]
    # Alternate who hosts, so home-field advantage is identified rather than
    # confounded with team strength.
    return [
        (a, b) if (week + i) % 2 == 0 else (b, a)
        for i, (a, b) in enumerate(zip(left, right))
    ]


def build(
    conn,
    *,
    seed: int = 7,
    seasons=SEASONS,
    noise: float = 6.0,
    brand_premium: float = PLANTED_BRAND_PREMIUM,
) -> dict:
    """Populate every source table. Returns a count of what was written.

    ``brand_premium`` plants the blue-blood effect: the market's number is
    inflated in the prestigious side's favour by this many points per standard
    deviation of prestige gap, so results systematically fall short of it.
    That gives ``analysis.brand_premium`` a known answer to recover.
    """
    rng = np.random.default_rng(seed)
    prestige = prestige_z()

    teams = [
        {
            "school": team,
            "mascot": f"{team}s",
            "conference": "Test",
            "classification": "fbs",
        }
        for team in TEAMS
    ]

    games: list[dict] = []
    drives: list[dict] = []
    plays: list[dict] = []
    lines: list[dict] = []
    recruiting: list[dict] = []
    talent: list[dict] = []
    portal: list[dict] = []
    game_id = 1_000_000

    for season in seasons:
        for team in TEAMS:
            recruiting.append(
                {
                    "season": season,
                    "school": team,
                    "rank": TEAMS.index(team) + 1,
                    "points": RECRUITING_POINTS[team],
                }
            )
            talent.append(
                {
                    "season": season,
                    "school": team,
                    "talent": RECRUITING_POINTS[team] * 3,
                }
            )
        portal.extend(_portal(rng, season, prestige))

        for week in WEEKS:
            for slot, (home, away) in enumerate(_pairings(week)):
                game_id += 1
                expected = TRUE_RATINGS[home] - TRUE_RATINGS[away] + TRUE_HFA
                margin = expected + rng.normal(0.0, noise)
                home_points, away_points = _scoreline(margin, rng)

                games.append(
                    {
                        "game_id": game_id,
                        "season": season,
                        "week": week,
                        "season_type": "regular",
                        "start_date": _kickoff(season, week, slot),
                        "neutral_site": False,
                        "conference_game": True,
                        "completed": True,
                        "home_team": home,
                        "away_team": away,
                        "home_conference": "Test",
                        "away_conference": "Test",
                        "home_classification": "fbs",
                        "away_classification": "fbs",
                        "home_points": home_points,
                        "away_points": away_points,
                        "venue": f"{home} Field",
                    }
                )
                # A market that knows the true ratings but not the noise, and
                # rounds to the half point the way a real book does — except
                # that it overpays for brand by `brand_premium` per SD of gap.
                gap = prestige[home] - prestige[away]
                priced = expected - brand_premium * gap
                lines.extend(
                    _lines(rng, game_id, season, priced)
                )
                drives.extend(_drives(rng, game_id, season, home, away))
                plays.extend(_plays(rng, game_id, season, home, away))

    tables = {
        "teams": teams,
        "games": games,
        "drives": drives,
        "plays": plays,
        "lines": lines,
        "recruiting": recruiting,
        "talent": talent,
        "portal": portal,
    }
    for table, rows in tables.items():
        conn.execute(f"DELETE FROM {table}")
        _insert(conn, table, rows)
    return {name: len(rows) for name, rows in tables.items()}


def _portal(
    rng,
    season: int,
    prestige: dict[str, float],
    *,
    teams: list[str] | None = None,
    count: int = 80,
) -> list[dict]:
    """Transfers, flowing out of prestigious programs and into the rest.

    The mechanism the NIL thesis requires: the bench at a blue blood empties
    into schools that will start those players. Planted with that direction so
    ``analysis.portal_and_prestige`` has a sign to recover.
    """
    teams = teams or TEAMS
    out = []
    leaving = np.array([max(0.15, 0.5 + 0.35 * prestige[team]) for team in teams])
    arriving = np.array([max(0.15, 0.5 - 0.35 * prestige[team]) for team in teams])
    leaving = leaving / leaving.sum()
    arriving = arriving / arriving.sum()
    for index in range(count):
        origin = str(rng.choice(teams, p=leaving))
        destination = str(rng.choice(teams, p=arriving))
        if origin == destination:
            continue
        out.append(
            {
                "season": season,
                "first_name": f"Player{index}",
                "last_name": origin,
                "position": "QB",
                "origin": origin,
                "destination": destination,
                "transfer_date": datetime(season, 1, 10),
                "rating": float(rng.uniform(0.75, 0.99)),
                "stars": int(rng.integers(2, 5)),
                "eligibility": "Junior",
            }
        )
    return out


def _drives(rng, game_id: int, season: int, home: str, away: str) -> list[dict]:
    """Eleven drives a side, worth more points as the field shortens."""
    out = []
    for side, (offense, defense) in enumerate(((home, away), (away, home))):
        offense_score = defense_score = 0
        for number in range(11):
            start = int(rng.integers(20, 96))
            # Roughly the real shape: about four points from inside the 20,
            # about one from your own 25, so a fitted curve has something to
            # recover rather than a flat line.
            chance = float(np.clip(0.9 - start / 130.0, 0.02, 0.9))
            points = 0
            if rng.random() < chance:
                points = 7 if rng.random() < 0.65 else 3
            new_offense = offense_score + points
            out.append(
                {
                    "drive_id": f"{game_id}-{side}-{number}",
                    "game_id": game_id,
                    "season": season,
                    "offense": offense,
                    "defense": defense,
                    "drive_number": number + 1,
                    "start_period": 1 + number // 3,
                    "start_yards_to_goal": start,
                    "end_yards_to_goal": max(0, start - int(rng.integers(0, start))),
                    "plays": int(rng.integers(3, 12)),
                    "yards": int(rng.integers(-4, 80)),
                    "drive_result": "TD" if points == 7 else ("FG" if points else "PUNT"),
                    "scoring": bool(points),
                    "start_offense_score": offense_score,
                    "start_defense_score": defense_score,
                    "end_offense_score": new_offense,
                    "end_defense_score": defense_score,
                }
            )
            offense_score = new_offense
    return out


def _plays(rng, game_id: int, season: int, home: str, away: str) -> list[dict]:
    out = []
    for side, (offense, defense) in enumerate(((home, away), (away, home))):
        skill = SALVAGE_SKILL[offense]
        for number in range(64):
            down = int(rng.integers(1, 5))
            distance = int(rng.integers(1, 16))
            if rng.random() < 0.5:
                gained = int(round(rng.normal(4.4, 5.0)))
                if gained <= 0:
                    # The whole point of the metric: a team with salvage skill
                    # gives up less ground once the play is already dead.
                    gained = int(round(min(0.0, gained + skill)))
                play_type = "Rush"
            elif rng.random() < 0.12:
                gained = -int(rng.integers(3, 11))
                play_type = "Sack"
            elif rng.random() < 0.06:
                # Turnovers, so `turnover_margin` is exercised rather than
                # uniformly zero. Both labels CFBD uses, so the play-type
                # pattern matching is exercised too.
                gained = 0
                play_type = (
                    "Pass Interception Return"
                    if rng.random() < 0.5
                    else "Fumble Recovery (Opponent)"
                )
            else:
                gained = int(round(rng.normal(6.5, 9.0)))
                play_type = "Pass Reception" if gained > 0 else "Pass Incompletion"
            out.append(
                {
                    "play_id": f"{game_id}-{side}-{number}",
                    "game_id": game_id,
                    "drive_id": f"{game_id}-{side}-{number // 6}",
                    "season": season,
                    "offense": offense,
                    "defense": defense,
                    "period": 1 + number // 16,
                    "down": down,
                    "distance": distance,
                    "yards_to_goal": int(rng.integers(5, 95)),
                    "yards_gained": gained,
                    "play_type": play_type,
                    "scoring": False,
                    "ppa": float(gained) / 10.0,
                }
            )
    return out


def build_wide(
    conn,
    *,
    seed: int = 19,
    # Above the 30-team floor `portal_and_prestige` requires, and enough games
    # a week that a season clears the 150 the per-season premium needs.
    n_teams: int = 32,
    seasons=LONG_SEASONS,
    noise: float = 13.0,
    brand_premium: float = PLANTED_BRAND_PREMIUM,
    with_portal: bool = True,
) -> dict:
    """A league wide enough for the per-season analyses, and no deeper.

    ``brand_premium``'s season-by-season series — the NIL drift, the thing the
    whole thesis turns on — needs 150 games in a season before it will report
    one, which needs far more teams than the eight the model tests use. Only
    the scoreboard and the line matter to those regressions, so this skips
    play-by-play entirely: a wide, shallow league instead of a narrow, deep one.
    """
    rng = np.random.default_rng(seed)
    teams = [f"Team {i:02d}" for i in range(n_teams)]
    ratings = {team: float(rng.normal(0, 9)) for team in teams}
    points = {team: float(rng.uniform(150, 300)) for team in teams}
    values = np.array([points[team] for team in teams])
    prestige = dict(
        zip(teams, (values - values.mean()) / values.std(ddof=0))
    )

    rows_teams = [
        {"school": t, "mascot": None, "conference": "Wide", "classification": "fbs"}
        for t in teams
    ]
    games, lines, recruiting, portal = [], [], [], []
    game_id = 5_000_000
    for season in seasons:
        for team in teams:
            recruiting.append(
                {
                    "season": season,
                    "school": team,
                    "rank": 0,
                    "points": points[team],
                }
            )
        if with_portal:
            portal.extend(_portal(rng, season, prestige, teams=teams, count=200))
        for week in WEEKS:
            order = list(rng.permutation(teams))
            for slot in range(0, len(order) - 1, 2):
                home, away = order[slot], order[slot + 1]
                game_id += 1
                expected = ratings[home] - ratings[away] + TRUE_HFA
                margin = expected + rng.normal(0.0, noise)
                home_points, away_points = _scoreline(margin, rng)
                gap = prestige[home] - prestige[away]
                priced = expected - brand_premium * gap
                games.append(
                    {
                        "game_id": game_id,
                        "season": season,
                        "week": week,
                        "season_type": "regular",
                        "start_date": _kickoff(season, week, slot),
                        "neutral_site": False,
                        "conference_game": True,
                        "completed": True,
                        "home_team": home,
                        "away_team": away,
                        "home_conference": "Wide",
                        "away_conference": "Wide",
                        "home_classification": "fbs",
                        "away_classification": "fbs",
                        "home_points": home_points,
                        "away_points": away_points,
                        "venue": home,
                    }
                )
                lines.extend(_lines(rng, game_id, season, priced))

    for table in (
        "teams", "games", "drives", "plays", "lines", "recruiting", "talent", "portal"
    ):
        conn.execute(f"DELETE FROM {table}")
    _insert(conn, "teams", rows_teams)
    _insert(conn, "games", games)
    _insert(conn, "lines", lines)
    _insert(conn, "recruiting", recruiting)
    _insert(conn, "portal", portal)
    return {"games": len(games), "teams": len(teams), "portal": len(portal)}


def history_frame(*, seed: int = 3, seasons=SEASONS, noise: float = 6.0) -> pd.DataFrame:
    """The same universe as a model-ready frame, with no database involved.

    Model tests want to check that a fit recovers known ratings; going through
    DuckDB to do that would test the SQL as well and blame the model when the
    SQL was wrong.
    """
    rng = np.random.default_rng(seed)
    rows = []
    game_id = 0
    for season in seasons:
        for week in WEEKS:
            for slot, (home, away) in enumerate(_pairings(week)):
                game_id += 1
                expected = TRUE_RATINGS[home] - TRUE_RATINGS[away] + TRUE_HFA
                margin = expected + rng.normal(0.0, noise)
                rows.append(
                    {
                        "game_id": game_id,
                        "season": season,
                        "week": week,
                        "kickoff": _kickoff(season, week, slot),
                        "home_team": home,
                        "away_team": away,
                        "neutral_site": False,
                        "margin": margin,
                        # A clean split: two thirds of the margin is earned,
                        # one third is where the drives started.
                        "efficiency_margin": margin * 2.0 / 3.0,
                        "fp_margin_pts": margin / 3.0,
                        "market_margin": round(expected * 2) / 2,
                        "prestige_gap": 0.0,
                    }
                )
    frame = pd.DataFrame(rows)
    frame["kickoff"] = pd.to_datetime(frame["kickoff"], utc=True)
    return frame


def _insert(conn, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    frame = pd.DataFrame(rows)
    columns = ", ".join(f'"{c}"' for c in frame.columns)
    conn.register("_synth", frame)
    try:
        conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _synth")
    finally:
        conn.unregister("_synth")
