"""Turning a model spread into a bet, or into a decision not to bet.

An edge in points is not yet a reason to bet. Three things stand between them:
the price (a 1.5-point edge at -125 is worse than no edge at -110), the vig
(the two sides of a market sum to more than 100%, and the excess is the book's,
not information), and the model's own error bar (a 3-point edge from a model
with a 13-point standard error is a coin flip with a slight lean).

This module does those three conversions and nothing else.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import DEFAULT_PROVIDER, american_to_profit, fbs_teams, load_frame
from .config import settings
from .db import cursor
from .features.prestige import prestige_gap, team_season_prestige
from .models import build_model

log = logging.getLogger(__name__)

# Standard deviation of (actual margin - model margin), in points. College
# football margins scatter widely: even a very good model lands about 13 points
# from the result on average. This is the number that keeps position sizes
# sane, and it is overridden from a backtest's measured error whenever one is
# available — see `sigma_from_backtests`.
DEFAULT_SIGMA = 13.5

# Kelly assumes the win probability is known. It is not — it comes from a model
# with its own error — and full Kelly on a mis-estimated probability is how
# bankrolls die. A quarter is the conventional discount for that uncertainty,
# and it is applied by default rather than offered as an option.
DEFAULT_KELLY_FRACTION = 0.25


def implied_probability(american: float) -> float:
    """Probability a price implies, vig included."""
    if american is None or (isinstance(american, float) and math.isnan(american)):
        return float("nan")
    if american < 0:
        return abs(american) / (abs(american) + 100.0)
    return 100.0 / (american + 100.0)


def devig(home_price: float, away_price: float) -> tuple[float, float]:
    """Strip the book's margin from a two-sided market.

    The two implied probabilities sum to more than one; the excess is the hold.
    Dividing each by the total is the standard "multiplicative" removal — it
    assumes the hold is spread proportionally across both sides, which is
    close enough for spreads priced near even money and is what the industry
    quotes as the fair number.
    """
    home = implied_probability(home_price)
    away = implied_probability(away_price)
    total = home + away
    if not math.isfinite(total) or total <= 0:
        return (float("nan"), float("nan"))
    return (home / total, away / total)


def cover_probability(edge: float, sigma: float = DEFAULT_SIGMA) -> float:
    """Probability the side you like covers, given the edge in points.

    The result minus the model's prediction is treated as normal with mean zero
    and standard deviation ``sigma``, so the chance of covering by ``edge``
    points is just the normal CDF of ``edge / sigma``. Football margins are not
    quite normal — they pile up on 3, 7, 10 and 14 — but the approximation is
    good away from those key numbers, and where it is worst it is conservative.
    """
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    return 0.5 * (1.0 + math.erf(edge / (sigma * math.sqrt(2.0))))


def expected_value(probability: float, american: float) -> float:
    """Units returned per unit staked, on average, at this price."""
    profit = american_to_profit(american)
    return probability * profit - (1.0 - probability)


def kelly_stake(
    probability: float, american: float, fraction: float = DEFAULT_KELLY_FRACTION
) -> float:
    """Fraction of bankroll to stake. Zero when there is no advantage."""
    profit = american_to_profit(american)
    if profit <= 0:
        return 0.0
    edge = probability * profit - (1.0 - probability)
    if edge <= 0:
        return 0.0
    return max(0.0, (edge / profit) * fraction)


def sigma_from_backtests(default: float = DEFAULT_SIGMA) -> float:
    """Use the measured error of the most recent backtest, if there is one.

    A model's real standard error is an empirical fact about that model, and
    guessing it is the difference between sensible and reckless staking. For a
    roughly normal error, sigma is about 1.2533 times the mean absolute error,
    which the backtest already reports.
    """
    try:
        with cursor() as conn:
            row = conn.execute(
                """
                SELECT json_extract(metrics, '$.mae_model')
                FROM backtest_runs
                WHERE json_extract(metrics, '$.mae_model') IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception:  # noqa: BLE001 — a missing table must not break the page
        return default
    if not row or row[0] is None:
        return default
    try:
        mae = float(str(row[0]).strip('"'))
    except ValueError:
        return default
    if not 3.0 < mae < 30.0:
        return default
    return mae * 1.2533


def upcoming_fixtures(conn, days_ahead: int = 10) -> pd.DataFrame:
    """Scheduled games inside the window, with prestige attached."""
    fixtures = conn.execute(
        """
        SELECT game_id, season, week, start_date AS kickoff,
               home_team, away_team, neutral_site
        FROM games
        WHERE NOT completed
          AND start_date IS NOT NULL
          AND start_date >= now() - INTERVAL 6 HOUR
          AND start_date <= now() + (INTERVAL 1 DAY * ?)
        ORDER BY start_date
        """,
        [days_ahead],
    ).df()
    if fixtures.empty:
        return fixtures
    fixtures["kickoff"] = pd.to_datetime(fixtures["kickoff"], utc=True)

    prestige = team_season_prestige(conn)
    if prestige.empty:
        fixtures["prestige_gap"] = 0.0
    else:
        fixtures = prestige_gap(prestige, fixtures)
    return fixtures


def live_spreads(conn) -> pd.DataFrame:
    """Latest spread prices per game per book, as a home-perspective row.

    The Odds API returns one outcome per team; this pivots them into a single
    row so a book's home number and away number can be de-vigged against each
    other.
    """
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT *, row_number() OVER (
                       PARTITION BY event_id, book, outcome
                       ORDER BY fetched_at DESC
                   ) AS recency
            FROM live_odds
            WHERE market = 'spreads'
        )
        SELECT event_id, commence_time, home_team, away_team, book,
               outcome, price, point, fetched_at
        FROM ranked WHERE recency = 1
        """
    ).df()
    if rows.empty:
        return rows

    home_side = rows[rows["outcome"] == rows["home_team"]].rename(
        columns={"price": "home_price", "point": "home_point"}
    )
    away_side = rows[rows["outcome"] == rows["away_team"]].rename(
        columns={"price": "away_price", "point": "away_point"}
    )
    merged = home_side.merge(
        away_side[["event_id", "book", "away_price", "away_point"]],
        on=["event_id", "book"],
        how="inner",
    )
    merged["commence_time"] = pd.to_datetime(merged["commence_time"], utc=True)
    # A book's home handicap is the spread; the market's expected home margin
    # is its negation, same convention as everywhere else.
    merged["market_margin"] = -merged["home_point"]
    return merged[
        [
            "event_id", "commence_time", "home_team", "away_team", "book",
            "home_price", "home_point", "away_price", "away_point",
            "market_margin", "fetched_at",
        ]
    ]


@dataclass
class EdgeConfig:
    model: str = ""
    params: dict | None = None
    threshold: float = 0.0
    sigma: float = 0.0
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    bankroll: float = 1000.0
    days_ahead: int = 10
    # Falls back to CFBD's stored line when no live odds have been polled,
    # so the page is useful before an Odds API key is wired up.
    allow_cfbd_fallback: bool = True


def compute_edges(config: EdgeConfig | None = None) -> pd.DataFrame:
    """Every upcoming game where the model disagrees with a book.

    One row per game per book: the same game can be a bet at one book and not
    at another, which is not a rounding artefact but the single most reliable
    edge available to a retail bettor. Half a point of spread is worth roughly
    1.5% of win probability, and the two books here regularly differ by more.
    """
    cfg = settings()
    config = config or EdgeConfig()
    model_name = config.model or cfg.default_model
    params = config.params if config.params is not None else {
        "half_life_days": cfg.default_half_life_days,
        "ridge_lambda": cfg.default_ridge_lambda,
    }
    threshold = config.threshold or cfg.default_edge_threshold
    sigma = config.sigma or sigma_from_backtests()

    with cursor() as conn:
        history = load_frame(conn, DEFAULT_PROVIDER)
        fixtures = upcoming_fixtures(conn, config.days_ahead)
        books = live_spreads(conn)
        fbs = fbs_teams(conn)

    if fixtures.empty:
        return pd.DataFrame()
    if history.empty:
        raise ValueError("No history to fit on — ingest and build features first.")

    if books.empty and config.allow_cfbd_fallback:
        books = _cfbd_fallback_lines(fixtures)

    # Prices are needed before the fit for market-anchored models, which take
    # the book's number as their starting point.
    priced = _attach_books(fixtures, books)
    if priced.empty:
        return pd.DataFrame()

    as_of = pd.Timestamp.now(tz="UTC")
    model = build_model(model_name, **params)
    model.fbs = fbs
    model.fit(history, as_of)

    # One prediction per game, then broadcast across that game's books — the
    # model does not know which book it is being compared with.
    per_game = priced.drop_duplicates(subset=["game_id"]).copy()
    per_game["model_margin"] = model.predict(per_game)
    priced = priced.merge(
        per_game[["game_id", "model_margin"]], on="game_id", how="left"
    )

    priced["edge"] = priced["model_margin"] - priced["market_margin"]
    priced["side"] = np.select(
        [priced["edge"] >= threshold, priced["edge"] <= -threshold],
        ["home", "away"],
        default="pass",
    )

    fair_home, fair_away = zip(
        *[
            devig(home, away)
            for home, away in zip(priced["home_price"], priced["away_price"])
        ]
    ) if len(priced) else ((), ())
    priced["fair_prob_home"] = fair_home
    priced["fair_prob_away"] = fair_away

    priced["price"] = np.where(
        priced["side"] == "away", priced["away_price"], priced["home_price"]
    )
    priced["bet_spread"] = np.where(
        priced["side"] == "away", priced["away_point"], priced["home_point"]
    )
    priced["market_fair_prob"] = np.where(
        priced["side"] == "away", priced["fair_prob_away"], priced["fair_prob_home"]
    )
    priced["model_prob"] = [
        cover_probability(abs(edge), sigma) if side != "pass" else float("nan")
        for edge, side in zip(priced["edge"], priced["side"])
    ]
    priced["ev_per_unit"] = [
        expected_value(prob, price) if np.isfinite(prob) and pd.notna(price) else np.nan
        for prob, price in zip(priced["model_prob"], priced["price"])
    ]
    priced["kelly_fraction"] = [
        kelly_stake(prob, price, config.kelly_fraction)
        if np.isfinite(prob) and pd.notna(price)
        else 0.0
        for prob, price in zip(priced["model_prob"], priced["price"])
    ]
    priced["stake"] = (priced["kelly_fraction"] * config.bankroll).round(2)
    priced["bet_team"] = np.where(
        priced["side"] == "away", priced["away_team"], priced["home_team"]
    )
    priced["sigma"] = sigma

    priced = priced.sort_values(
        ["kickoff", "ev_per_unit"], ascending=[True, False]
    ).reset_index(drop=True)
    return priced


def best_price_per_game(edges: pd.DataFrame) -> pd.DataFrame:
    """Collapse to the single best book for each game's recommended side.

    Line shopping expressed as a table: for every game with a bet, this is the
    book offering the most favourable number, and how much better it is than
    the worst one on offer.
    """
    if edges.empty:
        return edges
    bets = edges[edges["side"] != "pass"].copy()
    if bets.empty:
        return bets
    bets["rank"] = bets.groupby(["game_id", "side"])["ev_per_unit"].rank(
        ascending=False, method="first"
    )
    best = bets[bets["rank"] == 1].drop(columns=["rank"])
    spread_range = (
        bets.groupby(["game_id", "side"])["bet_spread"]
        .agg(["min", "max"])
        .reset_index()
        .rename(columns={"min": "worst_spread", "max": "best_spread"})
    )
    best = best.merge(spread_range, on=["game_id", "side"], how="left")
    best["shop_gain_pts"] = (best["best_spread"] - best["worst_spread"]).abs()
    return best.sort_values("ev_per_unit", ascending=False).reset_index(drop=True)


def _attach_books(fixtures: pd.DataFrame, books: pd.DataFrame) -> pd.DataFrame:
    if books.empty:
        return pd.DataFrame()
    merged = fixtures.merge(
        books, on=["home_team", "away_team"], how="inner", suffixes=("", "_book")
    )
    if merged.empty:
        return merged
    if "commence_time" in merged.columns:
        # Two teams can meet twice in a season (a rematch in a conference
        # championship). Requiring the book's kickoff to be within two days of
        # the scheduled one keeps a title-game price off the regular-season
        # meeting.
        delta = (merged["commence_time"] - merged["kickoff"]).abs()
        merged = merged[delta <= pd.Timedelta(days=2)]
    return merged


def _cfbd_fallback_lines(fixtures: pd.DataFrame) -> pd.DataFrame:
    """Use CFBD's stored line when no live odds are available.

    Consolation only: it is often stale and never carries Fanatics. It exists
    so the page shows something meaningful before an Odds API key is wired in.
    """
    with cursor() as conn:
        rows = conn.execute(
            """
            SELECT l.game_id, l.provider AS book, l.spread,
                   l.home_moneyline, l.away_moneyline
            FROM lines l
            WHERE l.game_id IN (SELECT game_id FROM games WHERE NOT completed)
              AND l.spread IS NOT NULL
            """
        ).df()
    if rows.empty:
        return rows
    rows = rows.merge(
        fixtures[["game_id", "home_team", "away_team", "kickoff"]],
        on="game_id",
        how="inner",
    )
    rows["home_point"] = rows["spread"]
    rows["away_point"] = -rows["spread"]
    # No prices in CFBD's stored lines, so assume a standard two-way -110.
    rows["home_price"] = -110
    rows["away_price"] = -110
    rows["market_margin"] = -rows["spread"]
    rows["commence_time"] = rows["kickoff"]
    rows["event_id"] = rows["game_id"].astype(str)
    rows["fetched_at"] = pd.NaT
    return rows[
        [
            "event_id", "commence_time", "home_team", "away_team", "book",
            "home_price", "home_point", "away_price", "away_point",
            "market_margin", "fetched_at",
        ]
    ]
