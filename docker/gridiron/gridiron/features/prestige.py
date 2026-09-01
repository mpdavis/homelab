"""Prestige, talent and portal churn — the inputs to the blue-blood thesis.

These are keyed by ``(season, team)`` rather than by game, so they are not
feature blocks and do not live in ``team_game``. They feed two things: optional
model covariates, and the brand-premium regression in
:mod:`gridiron.analysis`, which is where the thesis actually gets tested.

THE THESIS, STATED SO IT CAN BE WRONG. A blue blood's advantage has
historically come from two places: better starters, and far better depth —
the four-star who would have started anywhere sitting third on Alabama's
bench. NIL and a frictionless portal price that bench seat honestly for the
first time, so the depth advantage leaks out to schools that will start him.
If the market still prices the brand as though the depth were intact, blue
bloods are systematically overvalued and the teams absorbing that talent are
systematically undervalued.

Every clause of that is measurable here: prestige from recruiting history,
depth outflow from the portal table, and the market's willingness to pay for
the brand from the regression in `analysis.brand_premium`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Half-life in recruiting classes for the prestige index. Four years is one
# roster turnover: a program that stops recruiting well is no longer a blue
# blood by the time its last good class has graduated, which is about right.
PRESTIGE_HALF_LIFE_CLASSES = 4.0


def _zero_filled(frame: pd.DataFrame, column: str) -> pd.Series:
    """A numeric column with missing values read as zero.

    Going through ``to_numeric`` rather than a bare ``fillna`` matters: a left
    join that matched nothing produces an all-null column of *object* dtype,
    and filling that in place silently downcasts it, which pandas has
    deprecated. Coercing first states the intended type outright.
    """
    if column not in frame.columns:
        return pd.Series(0.0, index=frame.index)
    return pd.to_numeric(frame[column], errors="coerce").fillna(0.0)


def team_season_prestige(conn, seasons: list[int] | None = None) -> pd.DataFrame:
    """Prestige, talent and portal flow per team-season.

    Columns:

    ``recruiting_ewma``
        Exponentially weighted recruiting points over classes *strictly before*
        this season. Point-in-time by construction — a season never sees its
        own class, let alone a later one.
    ``prestige``
        That, standardised across the FBS teams playing that season. Roughly
        "how many standard deviations of brand", so it is comparable across
        eras in which the raw points scale has drifted.
    ``talent``
        CFBD's 247 roster composite for the season, when available — the
        stock of talent rather than the flow.
    ``portal_net_rating``
        Incoming transfer ratings minus outgoing. Negative for a program
        exporting its bench, which is the mechanism the thesis rests on.
    """
    recruiting = conn.execute(
        "SELECT season, school AS team, points FROM recruiting "
        "WHERE points IS NOT NULL"
    ).df()

    if recruiting.empty:
        return pd.DataFrame(
            columns=[
                "season", "team", "recruiting_ewma", "prestige", "talent",
                "portal_in_rating", "portal_out_rating", "portal_net_rating",
            ]
        )

    decay = 0.5 ** (1.0 / PRESTIGE_HALF_LIFE_CLASSES)
    frames = []
    all_seasons = sorted(recruiting["season"].unique())
    target_seasons = seasons or all_seasons
    for season in target_seasons:
        prior = recruiting[recruiting["season"] < season].copy()
        if prior.empty:
            continue
        prior["weight"] = decay ** (season - prior["season"])
        grouped = prior.groupby("team").apply(
            lambda g: np.average(g["points"], weights=g["weight"]),
            include_groups=False,
        )
        frames.append(
            pd.DataFrame(
                {
                    "season": season,
                    "team": grouped.index,
                    "recruiting_ewma": grouped.to_numpy(),
                }
            )
        )

    if not frames:
        return pd.DataFrame(
            columns=[
                "season", "team", "recruiting_ewma", "prestige", "talent",
                "portal_in_rating", "portal_out_rating", "portal_net_rating",
            ]
        )

    out = pd.concat(frames, ignore_index=True)
    # Standardise within season: the raw points scale has drifted upward over
    # the years, and an index that drifts with it would read as "everyone got
    # more prestigious" rather than as a ranking.
    out["prestige"] = out.groupby("season")["recruiting_ewma"].transform(
        lambda s: (s - s.mean()) / s.std(ddof=0) if s.std(ddof=0) > 0 else 0.0
    )

    talent = conn.execute("SELECT season, school AS team, talent FROM talent").df()
    if not talent.empty:
        out = out.merge(talent, on=["season", "team"], how="left")
    else:
        out["talent"] = np.nan

    out = out.merge(portal_flow(conn), on=["season", "team"], how="left")
    for column in ("portal_in_rating", "portal_out_rating", "portal_net_rating"):
        out[column] = _zero_filled(out, column)
    return out


def portal_flow(conn) -> pd.DataFrame:
    """Transfer talent in and out, per team-season.

    A player with no rating still counts as a body, but only rated players move
    the totals — an unrated walk-on transferring is not the phenomenon the
    thesis is about.
    """
    incoming = conn.execute(
        """
        SELECT season, destination AS team,
               sum(coalesce(rating, 0)) AS portal_in_rating,
               count(*)                 AS portal_in_count
        FROM portal
        WHERE destination IS NOT NULL
        GROUP BY 1, 2
        """
    ).df()
    outgoing = conn.execute(
        """
        SELECT season, origin AS team,
               sum(coalesce(rating, 0)) AS portal_out_rating,
               count(*)                 AS portal_out_count
        FROM portal
        WHERE origin IS NOT NULL
        GROUP BY 1, 2
        """
    ).df()

    if incoming.empty and outgoing.empty:
        # Typed, not just named. An untyped empty frame merges into object
        # columns downstream, and every numeric operation on them is either a
        # deprecation warning or a wrong answer.
        return pd.DataFrame(
            {
                "season": pd.Series(dtype="int64"),
                "team": pd.Series(dtype="object"),
                "portal_in_rating": pd.Series(dtype="float64"),
                "portal_out_rating": pd.Series(dtype="float64"),
                "portal_in_count": pd.Series(dtype="float64"),
                "portal_out_count": pd.Series(dtype="float64"),
                "portal_net_rating": pd.Series(dtype="float64"),
            }
        )

    flow = incoming.merge(outgoing, on=["season", "team"], how="outer")
    for column in (
        "portal_in_rating", "portal_out_rating",
        "portal_in_count", "portal_out_count",
    ):
        flow[column] = _zero_filled(flow, column)
    flow["portal_net_rating"] = flow["portal_in_rating"] - flow["portal_out_rating"]
    return flow


def prestige_gap(prestige: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """Attach ``prestige_gap`` (home minus away) to a frame of games.

    ``games`` needs ``season``, ``home_team`` and ``away_team``. Teams with no
    recruiting history — FCS opponents, mostly — get a prestige of zero, which
    reads as "league average brand" and is wrong in a known direction. The
    brand-premium analysis drops non-FBS games for exactly that reason.
    """
    lookup = prestige[["season", "team", "prestige"]]
    out = games.merge(
        lookup.rename(columns={"team": "home_team", "prestige": "home_prestige"}),
        on=["season", "home_team"],
        how="left",
    ).merge(
        lookup.rename(columns={"team": "away_team", "prestige": "away_prestige"}),
        on=["season", "away_team"],
        how="left",
    )
    out["home_prestige"] = _zero_filled(out, "home_prestige")
    out["away_prestige"] = _zero_filled(out, "away_prestige")
    out["prestige_gap"] = out["home_prestige"] - out["away_prestige"]
    return out
