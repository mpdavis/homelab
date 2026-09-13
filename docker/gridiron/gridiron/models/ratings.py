"""Rating models: weighted ridge, and the hidden-yardage decomposition.

The workhorse is a regularised, recency-weighted least-squares rating — what
you would get by taking a simple rating system, giving old games less weight,
and shrinking every team toward the league average by an amount that depends on
how little you have seen of them.

Ridge is not decoration here, it is what makes the problem solvable at all. A
rating system built on margins is unidentified: add ten points to every team's
rating and every predicted margin is unchanged. The penalty pins the ratings
near zero and picks one answer out of that family, and it does the shrinkage
that stops a 2-0 team with two blowouts from being rated the best in the
country.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import neutral_mask, register_model
from .weighting import Weighting

log = logging.getLogger(__name__)

# Everything outside the FBS is pooled into one pseudo-team. Roughly 120 FCS
# programs appear once each in a season of FBS schedules, and rating each of
# them off a single game produces 120 meaningless parameters and a noisier fit
# for everyone they played. One shared rating for "an FCS opponent" is both
# more accurate and closer to what the number means.
FCS = "__FCS__"

# Below this many training games the fit is not worth trusting; the backtester
# skips weeks that cannot clear it rather than reporting confident nonsense
# from the opening Saturday of the sample.
MIN_TRAIN_GAMES = 150


def _pool_non_fbs(teams: pd.Series, fbs: set[str] | None) -> pd.Series:
    if not fbs:
        return teams
    return teams.where(teams.isin(fbs), FCS)


class RidgeRatings:
    """Weighted ridge team ratings on one response column.

    Not a registered model on its own — it is the engine the registered models
    are built from, so that "rate margin" and "rate field position separately
    with harder shrinkage" share one implementation.
    """

    def __init__(
        self,
        response: str,
        weighting: Weighting,
        ridge_lambda: float,
        *,
        fit_hfa: bool = True,
    ):
        self.response = response
        self.weighting = weighting
        self.ridge_lambda = float(ridge_lambda)
        self.fit_hfa = fit_hfa
        self.ratings: dict[str, float] = {}
        self.hfa: float = 0.0
        self.n_train: int = 0
        self.fbs: set[str] | None = None

    def fit(self, history: pd.DataFrame, as_of) -> "RidgeRatings":
        frame = history.dropna(subset=[self.response, "kickoff"]).copy()
        if frame.empty:
            self.ratings, self.hfa, self.n_train = {}, 0.0, 0
            return self

        # THE LEAKAGE GUARD. Everything downstream trusts that this happened.
        cutoff = pd.Timestamp(as_of)
        kickoffs = pd.to_datetime(frame["kickoff"], utc=True)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        frame = frame[kickoffs < cutoff]
        if frame.empty:
            self.ratings, self.hfa, self.n_train = {}, 0.0, 0
            return self
        kickoffs = pd.to_datetime(frame["kickoff"], utc=True)

        ages = (cutoff - kickoffs).dt.total_seconds().to_numpy() / 86400.0
        if "season" in frame.columns:
            seasons_back = frame["season"].max() - frame["season"].to_numpy()
        else:
            seasons_back = None
        weights = self.weighting.weights(ages, seasons_back)

        keep = weights > 1e-9
        frame, weights = frame[keep], weights[keep]
        if frame.empty:
            self.ratings, self.hfa, self.n_train = {}, 0.0, 0
            return self

        home = _pool_non_fbs(frame["home_team"], self.fbs).to_numpy()
        away = _pool_non_fbs(frame["away_team"], self.fbs).to_numpy()
        teams = sorted(set(home) | set(away))
        index = {team: i for i, team in enumerate(teams)}
        n, t = len(frame), len(teams)

        # Columns: one per team, then one for home-field advantage.
        width = t + (1 if self.fit_hfa else 0)
        design = np.zeros((n, width))
        rows = np.arange(n)
        # np.add.at rather than assignment: if both sides pool to FCS the two
        # writes must cancel to zero, not overwrite each other.
        np.add.at(design, (rows, np.array([index[x] for x in home])), 1.0)
        np.add.at(design, (rows, np.array([index[x] for x in away])), -1.0)
        if self.fit_hfa:
            design[:, t] = np.where(neutral_mask(frame), 0.0, 1.0)

        y = frame[self.response].to_numpy(dtype=float)
        sqrt_w = np.sqrt(weights)
        xw = design * sqrt_w[:, None]
        yw = y * sqrt_w

        penalty = np.eye(width) * self.ridge_lambda
        if self.fit_hfa:
            # Home advantage is a real, well-measured effect with one parameter
            # and thousands of observations behind it. Shrinking it toward zero
            # would only bias it, so it is left effectively unpenalised — the
            # residual jitter is there to keep the solve conditioned if a slice
            # happens to contain nothing but neutral-site games.
            penalty[t, t] = 1e-6

        gram = xw.T @ xw + penalty
        moment = xw.T @ yw
        try:
            beta = np.linalg.solve(gram, moment)
        except np.linalg.LinAlgError:
            beta, *_ = np.linalg.lstsq(gram, moment, rcond=None)

        self.ratings = {team: float(beta[i]) for team, i in index.items()}
        self.hfa = float(beta[t]) if self.fit_hfa else 0.0
        self.n_train = n
        return self

    def rating(self, team: str) -> float:
        if team in self.ratings:
            return self.ratings[team]
        # An unseen team is an FCS opponent or a first-year program: the pooled
        # rating is a much better guess than league average.
        return self.ratings.get(FCS, 0.0)

    def predict(self, fixtures: pd.DataFrame) -> np.ndarray:
        if not self.ratings:
            return np.full(len(fixtures), np.nan)
        home = _pool_non_fbs(fixtures["home_team"], self.fbs)
        away = _pool_non_fbs(fixtures["away_team"], self.fbs)
        neutral = neutral_mask(fixtures)
        home_rating = np.array([self.rating(x) for x in home])
        away_rating = np.array([self.rating(x) for x in away])
        return home_rating - away_rating + np.where(neutral, 0.0, self.hfa)

    def table(self) -> pd.DataFrame:
        return (
            pd.DataFrame(
                {"team": list(self.ratings), "rating": list(self.ratings.values())}
            )
            .sort_values("rating", ascending=False)
            .reset_index(drop=True)
        )


@register_model(
    "ridge_margin",
    "Recency-weighted ridge ratings on raw scoring margin — the baseline",
)
class RidgeMarginModel:
    """Rate teams on the scoreboard and nothing else.

    Every other model has to beat this, and it is a much stronger opponent than
    it looks: scoring margin is the single best cheap predictor in the sport.
    """

    name = "ridge_margin"

    def __init__(
        self,
        half_life_days: float = 240.0,
        ridge_lambda: float = 12.0,
        season_carryover: float = 0.65,
        weighting_kind: str = "exponential",
        window_days: float | None = None,
        **_,
    ):
        self.weighting = Weighting(
            kind=weighting_kind,
            half_life_days=half_life_days,
            window_days=window_days,
            season_carryover=season_carryover,
        )
        self.engine = RidgeRatings("margin", self.weighting, ridge_lambda)

    def fit(self, history: pd.DataFrame, as_of) -> None:
        self.engine.fbs = getattr(self, "fbs", None)
        self.engine.fit(history, as_of)

    def predict(self, fixtures: pd.DataFrame) -> np.ndarray:
        return self.engine.predict(fixtures)

    @property
    def n_train(self) -> int:
        return self.engine.n_train

    def describe(self) -> dict:
        return {
            "weighting": self.weighting.describe(),
            "hfa": round(self.engine.hfa, 2),
            "n_train": self.engine.n_train,
        }


@register_model(
    "decomposed",
    "Hidden yardage: rate efficiency and field position separately, shrink them differently",
)
class DecomposedModel:
    """Split the margin, rate the halves on their own terms, add them back.

    This is the hidden-yardage thesis as a model. Scoring margin is the sum of
    two things with very different shelf lives:

    * ``efficiency_margin`` — the margin a team earned by moving the ball. It
      is sticky. A team that was efficient in October is usually efficient in
      November.
    * ``fp_margin_pts`` — the points a team was handed by where its drives
      started. Partly a real skill (punting, coverage, returns) and partly luck
      (a tipped ball, a muffed punt). It regresses hard.

    Rating them together, as ``ridge_margin`` does, forces one half-life and
    one amount of shrinkage onto both, which necessarily over-trusts the noisy
    half and under-trusts the stable one. Here each gets its own: field
    position is shrunk several times harder and given a shorter memory, which
    amounts to saying "we believe roughly a third of your field-position edge
    will repeat, and none of the part from three months ago".

    The two responses sum exactly to the scoring margin, so the two ratings sum
    to a margin prediction with no double counting and no missing term.
    """

    name = "decomposed"

    def __init__(
        self,
        half_life_days: float = 240.0,
        ridge_lambda: float = 12.0,
        # Field position gets its own knobs, defaulted to distrust it: a
        # shorter memory and much harder shrinkage than efficiency.
        fp_half_life_days: float | None = 150.0,
        fp_ridge_lambda: float = 45.0,
        season_carryover: float = 0.65,
        weighting_kind: str = "exponential",
        window_days: float | None = None,
        **_,
    ):
        self.efficiency_weighting = Weighting(
            kind=weighting_kind,
            half_life_days=half_life_days,
            window_days=window_days,
            season_carryover=season_carryover,
        )
        self.fp_weighting = Weighting(
            kind=weighting_kind,
            half_life_days=fp_half_life_days or half_life_days,
            window_days=window_days,
            season_carryover=season_carryover,
        )
        self.efficiency = RidgeRatings(
            "efficiency_margin", self.efficiency_weighting, ridge_lambda
        )
        # Only one of the two components should carry the home-field term, or
        # the prediction counts it twice. Efficiency keeps it: the reason home
        # teams win is mostly that they play better, not that they get better
        # starting spots.
        self.field_position = RidgeRatings(
            "fp_margin_pts", self.fp_weighting, fp_ridge_lambda, fit_hfa=False
        )

    def fit(self, history: pd.DataFrame, as_of) -> None:
        fbs = getattr(self, "fbs", None)
        self.efficiency.fbs = fbs
        self.field_position.fbs = fbs
        self.efficiency.fit(history, as_of)
        self.field_position.fit(history, as_of)

    def predict(self, fixtures: pd.DataFrame) -> np.ndarray:
        efficiency = self.efficiency.predict(fixtures)
        field_position = self.field_position.predict(fixtures)
        # A team with no field-position history still has an efficiency rating;
        # treating the missing half as zero is right, dropping the game is not.
        return efficiency + np.nan_to_num(field_position, nan=0.0)

    @property
    def n_train(self) -> int:
        return self.efficiency.n_train

    def describe(self) -> dict:
        return {
            "efficiency_weighting": self.efficiency_weighting.describe(),
            "fp_weighting": self.fp_weighting.describe(),
            "hfa": round(self.efficiency.hfa, 2),
            "n_train": self.efficiency.n_train,
        }


@register_model(
    "market_debias",
    "Take the market line and remove the brand premium it is measured to carry",
)
class MarketDebiasModel:
    """The blue-blood thesis as a bet, rather than as an observation.

    A closing line is a very good forecast — better than anything in this
    package will produce from scratch. Beating it from first principles is
    hard. Beating it by finding one thing it is *systematically* wrong about is
    a different and much more tractable problem.

    So this model does not build a rating at all. It takes the market's number
    and asks a single question of history: after the fact, did teams with more
    brand prestige beat the closing line, or fall short of it? That is one
    weighted regression of ``actual_margin - market_margin`` on the prestige
    gap. If the coefficient is negative, the market has been paying for the
    brand — the premium is real and it is worth this many points per standard
    deviation of prestige — and the fair line is the market's line with that
    premium taken back out.

    The coefficient is re-estimated at every point in time from data available
    then, so if NIL and the portal have been eroding the premium, the model
    tracks the erosion instead of assuming it. Watching that coefficient move
    season by season is the direct read on your hypothesis; the analysis page
    plots it.

    A caution worth stating plainly: this model's edges are, by construction,
    small and correlated with each other. Every bet it makes is the same bet on
    the same coefficient. Judge it on the sweep and the confidence interval,
    never on a good month.
    """

    name = "market_debias"

    def __init__(
        self,
        half_life_days: float = 800.0,
        season_carryover: float = 0.9,
        # Guard against fitting a premium off a handful of games early in a
        # backtest, where the coefficient is unstable and large.
        min_games: int = 400,
        max_adjustment: float = 7.0,
        **_,
    ):
        # A long memory on purpose: this estimates one number, so it wants as
        # much data as it can get, and a market bias moves on the timescale of
        # rule changes rather than of a hot streak.
        self.weighting = Weighting(
            kind="exponential",
            half_life_days=half_life_days,
            season_carryover=season_carryover,
        )
        self.min_games = min_games
        self.max_adjustment = max_adjustment
        self.premium: float = 0.0
        self.intercept: float = 0.0
        self.n_train: int = 0

    def fit(self, history: pd.DataFrame, as_of) -> None:
        needed = {"market_margin", "prestige_gap", "margin", "kickoff"}
        if not needed.issubset(history.columns):
            missing = sorted(needed - set(history.columns))
            raise ValueError(
                f"market_debias needs columns {missing}; the backtester supplies "
                "them, so this is a caller error"
            )
        frame = history.dropna(subset=["market_margin", "prestige_gap", "margin"]).copy()
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        kickoffs = pd.to_datetime(frame["kickoff"], utc=True)
        frame = frame[kickoffs < cutoff]

        if len(frame) < self.min_games:
            self.premium, self.intercept, self.n_train = 0.0, 0.0, len(frame)
            return

        kickoffs = pd.to_datetime(frame["kickoff"], utc=True)
        ages = (cutoff - kickoffs).dt.total_seconds().to_numpy() / 86400.0
        seasons_back = (
            frame["season"].max() - frame["season"].to_numpy()
            if "season" in frame.columns
            else None
        )
        weights = self.weighting.weights(ages, seasons_back)

        residual = (frame["margin"] - frame["market_margin"]).to_numpy(dtype=float)
        gap = frame["prestige_gap"].to_numpy(dtype=float)
        design = np.column_stack([np.ones_like(gap), gap])
        sqrt_w = np.sqrt(weights)
        beta, *_ = np.linalg.lstsq(design * sqrt_w[:, None], residual * sqrt_w, rcond=None)
        self.intercept, self.premium = float(beta[0]), float(beta[1])
        self.n_train = len(frame)

    def predict(self, fixtures: pd.DataFrame) -> np.ndarray:
        if "market_margin" not in fixtures.columns:
            return np.full(len(fixtures), np.nan)
        market = fixtures["market_margin"].to_numpy(dtype=float)
        gap = fixtures.get("prestige_gap")
        gap = (
            np.zeros(len(fixtures))
            if gap is None
            else pd.to_numeric(gap, errors="coerce").fillna(0.0).to_numpy(dtype=float)
        )
        adjustment = np.clip(
            self.intercept + self.premium * gap,
            -self.max_adjustment,
            self.max_adjustment,
        )
        return market + adjustment

    def describe(self) -> dict:
        return {
            "brand_premium_pts_per_sd": round(self.premium, 3),
            "intercept": round(self.intercept, 3),
            "n_train": self.n_train,
        }
