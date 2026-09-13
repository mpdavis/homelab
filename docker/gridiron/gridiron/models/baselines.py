"""Baselines. A model is only interesting relative to these.

``market`` in particular is not a throwaway. Backtesting it answers the
question every other number in this system has to be read against: how good is
the closing line, in points of mean absolute error? Everything else is judged
by how close it gets to that, and by whether its disagreements with it are
profitable — which are two different questions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import neutral_mask, register_model


@register_model("market", "The closing line itself — the benchmark to beat")
class MarketModel:
    """Predict exactly what the market predicted.

    Its edge against the market is zero by construction, so it never bets and
    its return is zero rather than negative. What it is for is the error
    columns: a backtest of this model reports the market's own accuracy, which
    is the yardstick for every other model's.
    """

    name = "market"

    def __init__(self, **_):
        self.n_train = 0

    def fit(self, history: pd.DataFrame, as_of) -> None:
        self.n_train = len(history)

    def predict(self, fixtures: pd.DataFrame) -> np.ndarray:
        if "market_margin" not in fixtures.columns:
            return np.full(len(fixtures), np.nan)
        return fixtures["market_margin"].to_numpy(dtype=float)

    def describe(self) -> dict:
        return {"note": "benchmark only; never produces an edge"}


@register_model(
    "elo", "Online margin ratings — recency by update rate rather than by half-life"
)
class EloModel:
    """A margin-aware Elo, kept as a contrast to the ridge models.

    Where the ridge models express recency as an explicit weight on old games,
    this expresses it as a learning rate: every game nudges a team's rating
    toward what would have predicted the result, so old games fade because they
    have been overwritten rather than because they were down-weighted.

    That difference matters for the question you actually asked. A half-life
    sweep and a learning-rate sweep are two ways of asking "how much history
    should count", and they disagree in an informative way — Elo adapts fast to
    a team that has genuinely changed (a quarterback injury) and the ridge fit
    adapts slowly but is far steadier week to week. If a theory only works
    under one of them, that is worth knowing before betting it.
    """

    name = "elo"

    def __init__(
        self,
        k: float = 0.11,
        hfa: float = 2.4,
        # Ratings carry over between seasons, but only partly: rosters turn
        # over and a 1.0 here would treat a team as unchanged across an
        # offseason. 0.6 is roughly what fits best in this sport.
        season_regression: float = 0.6,
        # Blowouts contain less information per point than close games do —
        # a 60-0 result is not four times the evidence of a 15-0 result.
        margin_cap: float = 28.0,
        # A sweep injects the ridge models' recency knobs into every model it
        # runs; this one expresses recency as `k` instead, so it accepts and
        # ignores them rather than failing the sweep point.
        **_,
    ):
        self.k = k
        self.hfa = hfa
        self.season_regression = season_regression
        self.margin_cap = margin_cap
        self.ratings: dict[str, float] = {}
        self.n_train = 0

    def fit(self, history: pd.DataFrame, as_of) -> None:
        frame = history.dropna(subset=["margin", "kickoff"]).copy()
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        kickoffs = pd.to_datetime(frame["kickoff"], utc=True)
        frame = frame[kickoffs < cutoff].sort_values("kickoff")

        self.ratings = {}
        self.n_train = len(frame)
        if frame.empty:
            return

        last_season = None
        for row in frame.itertuples(index=False):
            season = getattr(row, "season", None)
            if last_season is not None and season != last_season:
                # New season: pull everyone back toward the mean.
                self.ratings = {
                    team: rating * self.season_regression
                    for team, rating in self.ratings.items()
                }
            last_season = season

            home, away = row.home_team, row.away_team
            neutral = bool(getattr(row, "neutral_site", False))
            home_rating = self.ratings.get(home, 0.0)
            away_rating = self.ratings.get(away, 0.0)
            expected = home_rating - away_rating + (0.0 if neutral else self.hfa)
            actual = float(np.clip(row.margin, -self.margin_cap, self.margin_cap))
            surprise = actual - expected
            self.ratings[home] = home_rating + self.k * surprise
            self.ratings[away] = away_rating - self.k * surprise

    def predict(self, fixtures: pd.DataFrame) -> np.ndarray:
        if not self.ratings:
            return np.full(len(fixtures), np.nan)
        neutral = neutral_mask(fixtures)
        home = np.array([self.ratings.get(t, 0.0) for t in fixtures["home_team"]])
        away = np.array([self.ratings.get(t, 0.0) for t in fixtures["away_team"]])
        return home - away + np.where(neutral, 0.0, self.hfa)

    def describe(self) -> dict:
        return {"k": self.k, "hfa": self.hfa, "n_train": self.n_train}
