"""Theory diagnostics — asking whether an idea is true before betting it.

A backtest tells you whether a strategy made money in a sample. These functions
ask the prior question: is the mechanism it depends on actually there? The two
are different, and running only the first is how people end up betting a
coincidence.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .backtest import DEFAULT_PROVIDER, load_frame
from .db import cursor
from .features.prestige import team_season_prestige

log = logging.getLogger(__name__)


def _wls(design: np.ndarray, y: np.ndarray, weights: np.ndarray | None = None) -> dict:
    """Weighted least squares with standard errors, without pulling in statsmodels.

    Returns coefficients, their standard errors and t-statistics. The errors
    assume independent observations; games in the same week share a market
    regime, so the true errors are somewhat wider than these. Treat a t of 2 as
    suggestive rather than settled.
    """
    n, k = design.shape
    weights = np.ones(n) if weights is None else np.asarray(weights, dtype=float)
    sqrt_w = np.sqrt(weights)
    xw, yw = design * sqrt_w[:, None], y * sqrt_w

    gram = xw.T @ xw
    try:
        gram_inv = np.linalg.inv(gram)
    except np.linalg.LinAlgError:
        gram_inv = np.linalg.pinv(gram)
    beta = gram_inv @ (xw.T @ yw)

    residual = yw - xw @ beta
    dof = max(n - k, 1)
    sigma2 = float(residual @ residual) / dof
    cov = sigma2 * gram_inv
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    total = float(((yw - yw.mean()) ** 2).sum())
    r2 = 1.0 - float(residual @ residual) / total if total > 0 else 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        t_stats = np.where(se > 0, beta / se, np.nan)
    return {
        "beta": beta,
        "se": se,
        "t": t_stats,
        "n": int(n),
        "r2": float(r2),
        "sigma": float(np.sqrt(sigma2)),
    }


# ---------------------------------------------------------------------------
# Blue-blood bias
# ---------------------------------------------------------------------------


def brand_premium(provider: str = DEFAULT_PROVIDER) -> dict:
    """How many points the market pays for a brand, per season and overall.

    THE TEST. For every game, take the market's closing number and the result,
    and compute how far the result fell short of, or beat, the number. Regress
    that miss on the prestige gap between the two teams. If the market prices
    brands correctly, the coefficient is zero: prestige is already in the line,
    and knowing it tells you nothing more about the result.

    A NEGATIVE coefficient is your thesis. It says that when the home team is
    the more prestigious side, the result comes in *below* the line — the
    market set the number too high because it was paying for the name. The
    magnitude is in points per standard deviation of prestige, so a
    coefficient of -0.8 means a game between a two-sigma blue blood and an
    average program carries about 1.6 points of brand premium, which is a large
    number in a market that prices in half-points.

    THE DRIFT IS THE INTERESTING PART. The per-season series is what tests the
    NIL story specifically. If the premium was substantial through the late
    2010s and has shrunk since 2021, that is the depth advantage being
    arbitraged away by the portal, showing up in the one place it has to show
    up: the gap between what the market charges for the brand and what the
    brand now delivers.

    Non-FBS opponents are excluded. They have no recruiting history, so their
    prestige defaults to zero, which is not "average" but "unmeasured" — and
    they play the blue bloods disproportionately in September, which would
    load the estimate with exactly the games it is least able to speak to.
    """
    with cursor() as conn:
        games = load_frame(conn, provider)

    if games.empty:
        return {"error": "No games with features loaded."}

    frame = games.dropna(subset=["market_margin", "margin", "prestige_gap"]).copy()
    for column in ("home_classification", "away_classification"):
        if column in frame.columns:
            frame = frame[
                frame[column].isna()
                | frame[column].str.lower().eq("fbs")
            ]
    # A team with no recruiting history got a prestige of zero from the join
    # rather than a measurement; keep only games where both sides were measured.
    frame = frame[
        (frame["home_prestige"] != 0.0) | (frame["away_prestige"] != 0.0)
    ]
    if len(frame) < 200:
        return {"error": f"Only {len(frame)} usable games; need at least 200."}

    residual = (frame["margin"] - frame["market_margin"]).to_numpy(dtype=float)
    gap = frame["prestige_gap"].to_numpy(dtype=float)
    design = np.column_stack([np.ones_like(gap), gap])
    overall = _wls(design, residual)

    by_season = []
    for season, group in frame.groupby("season"):
        if len(group) < 150:
            continue
        season_residual = (group["margin"] - group["market_margin"]).to_numpy(float)
        season_gap = group["prestige_gap"].to_numpy(float)
        fit = _wls(
            np.column_stack([np.ones_like(season_gap), season_gap]), season_residual
        )
        by_season.append(
            {
                "season": int(season),
                "premium_pts_per_sd": round(float(fit["beta"][1]), 3),
                "se": round(float(fit["se"][1]), 3),
                "t": round(float(fit["t"][1]), 2),
                "games": fit["n"],
            }
        )

    # Where the premium actually lands: bucket games by how lopsided the
    # prestige matchup is and show the average miss in each bucket. A
    # regression coefficient can be dragged around by a handful of extreme
    # matchups; the buckets show whether the effect is monotone or is one tail.
    buckets = pd.cut(
        frame["prestige_gap"],
        bins=[-np.inf, -1.5, -0.5, 0.5, 1.5, np.inf],
        labels=[
            "home much less prestigious",
            "home less",
            "even",
            "home more",
            "home much more prestigious",
        ],
    )
    by_bucket = []
    for label, group in frame.groupby(buckets, observed=True):
        miss = (group["margin"] - group["market_margin"]).mean()
        cover_rate = float((group["margin"] > group["market_margin"]).mean())
        by_bucket.append(
            {
                "bucket": str(label),
                "games": int(len(group)),
                "mean_miss_pts": round(float(miss), 3),
                "home_cover_rate": round(cover_rate, 4),
            }
        )

    return {
        "premium_pts_per_sd": round(float(overall["beta"][1]), 3),
        "se": round(float(overall["se"][1]), 3),
        "t": round(float(overall["t"][1]), 2),
        "intercept": round(float(overall["beta"][0]), 3),
        "games": overall["n"],
        "interpretation": _interpret_premium(
            float(overall["beta"][1]), float(overall["t"][1])
        ),
        "by_season": by_season,
        "by_prestige_bucket": by_bucket,
    }


def _interpret_premium(beta: float, t: float) -> str:
    if abs(t) < 2:
        return (
            "Not distinguishable from zero. On this sample the market prices "
            "prestige about right, and a strategy built on the brand premium "
            "has nothing to stand on."
        )
    if beta < 0:
        return (
            f"The market overpays for the brand by roughly {abs(beta):.2f} points "
            "per standard deviation of prestige. Prestigious teams fall short of "
            "their closing numbers, which is the thesis — bet against the name."
        )
    return (
        f"The market UNDERpays for the brand by roughly {beta:.2f} points per "
        "standard deviation. This is the opposite of the thesis: prestigious "
        "teams have been beating their numbers, so fading them would have lost."
    )


def portal_and_prestige() -> pd.DataFrame:
    """Net transfer-portal talent flow against prestige, by season.

    The mechanism check for the NIL story. If depth really is leaking out of
    the blue bloods, the correlation between prestige and net portal rating
    should be negative and should have grown more negative since 2021 — the
    most prestigious programs exporting more talent than they import.
    """
    with cursor() as conn:
        prestige = team_season_prestige(conn)
    if prestige.empty:
        return prestige

    rows = []
    for season, group in prestige.groupby("season"):
        if len(group) < 30 or group["portal_net_rating"].abs().sum() == 0:
            continue
        gap = group["prestige"].to_numpy(float)
        net = group["portal_net_rating"].to_numpy(float)
        fit = _wls(np.column_stack([np.ones_like(gap), gap]), net)
        rows.append(
            {
                "season": int(season),
                "slope_rating_per_sd": round(float(fit["beta"][1]), 3),
                "t": round(float(fit["t"][1]), 2),
                "teams": fit["n"],
                "mean_net_top_quartile": round(
                    float(
                        group[group["prestige"] >= group["prestige"].quantile(0.75)][
                            "portal_net_rating"
                        ].mean()
                    ),
                    2,
                ),
                "mean_net_bottom_quartile": round(
                    float(
                        group[group["prestige"] <= group["prestige"].quantile(0.25)][
                            "portal_net_rating"
                        ].mean()
                    ),
                    2,
                ),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Hidden yardage
# ---------------------------------------------------------------------------


def hidden_yardage_persistence(min_games: int = 6) -> dict:
    """Split-half reliability: is hidden yardage a skill or is it noise?

    This is the question that decides how the decomposed model should be tuned,
    and it is answerable without any reference to betting. Split each team's
    season into odd-numbered and even-numbered games, average a metric over
    each half, and correlate the halves across teams. A metric that measures a
    real, stable property of a team correlates with itself; one that measures
    what happened to it does not.

    Expect efficiency to come out strongly reliable and field position much
    less so — and that gap is precisely the justification for shrinking the
    field-position ratings harder than the efficiency ratings. If field
    position turned out to be *as* reliable as efficiency, the decomposed
    model's defaults would be wrong and should be changed.
    """
    with cursor() as conn:
        frame = conn.execute(
            """
            SELECT season, team, kickoff,
                   row_number() OVER (
                       PARTITION BY season, team ORDER BY kickoff
                   ) AS game_number,
                   fp_margin_pts, efficiency_margin, margin,
                   salvage_yards_per_rush, success_rate, avg_start_yards_to_goal
            FROM team_game
            WHERE fp_margin_pts IS NOT NULL
            """
        ).df()

    if frame.empty:
        return {"error": "team_game is empty — run `gridiron features build`."}

    metrics = [
        "margin",
        "efficiency_margin",
        "fp_margin_pts",
        "success_rate",
        "salvage_yards_per_rush",
        "avg_start_yards_to_goal",
    ]
    frame["half"] = np.where(frame["game_number"] % 2 == 1, "odd", "even")

    counts = frame.groupby(["season", "team"]).size()
    eligible = counts[counts >= min_games].index
    frame = frame.set_index(["season", "team"]).loc[eligible].reset_index()

    results = []
    for metric in metrics:
        pivot = (
            frame.pivot_table(
                index=["season", "team"], columns="half", values=metric, aggfunc="mean"
            )
            .dropna()
        )
        if len(pivot) < 30:
            continue
        correlation = float(pivot["odd"].corr(pivot["even"]))
        # Split-half correlation understates full-season reliability because
        # each half is only half as long; Spearman-Brown corrects for that.
        adjusted = (2 * correlation) / (1 + correlation) if correlation > -1 else np.nan
        results.append(
            {
                "metric": metric,
                "split_half_r": round(correlation, 3),
                "full_season_r": round(float(adjusted), 3),
                "team_seasons": int(len(pivot)),
            }
        )

    results.sort(key=lambda row: row["split_half_r"], reverse=True)
    return {
        "min_games": min_games,
        "metrics": results,
        "interpretation": _interpret_persistence(results),
    }


def _interpret_persistence(results: list[dict]) -> str:
    lookup = {row["metric"]: row["split_half_r"] for row in results}
    efficiency = lookup.get("efficiency_margin")
    field_position = lookup.get("fp_margin_pts")
    if efficiency is None or field_position is None:
        return "Not enough data to compare the components."
    if field_position < efficiency - 0.1:
        return (
            f"Efficiency repeats ({efficiency:.2f}) considerably better than field "
            f"position ({field_position:.2f}). Shrinking the field-position "
            "ratings harder than the efficiency ratings — which is what the "
            "decomposed model does by default — is the right call on this data."
        )
    if field_position > efficiency:
        return (
            f"Field position repeats ({field_position:.2f}) at least as well as "
            f"efficiency ({efficiency:.2f}). That is a genuine surprise and it "
            "means the decomposed model's default of shrinking field position "
            "harder is wrong here: raise fp_half_life_days and lower "
            "fp_ridge_lambda before trusting its backtests."
        )
    return (
        f"The two components repeat similarly ({efficiency:.2f} vs "
        f"{field_position:.2f}); the decomposition is unlikely to add much over "
        "rating margin directly."
    )


def hidden_yardage_market_test(provider: str = DEFAULT_PROVIDER) -> dict:
    """Does the market already know about field position?

    The complement to the persistence test. Hidden yardage is only worth
    betting if it is *both* a repeatable property of a team *and* one the
    market has not already priced. This regresses the market's error on the
    field-position edge the two teams had built up before kickoff: if the
    coefficient is zero, the market has it priced and there is nothing here,
    however real the effect turns out to be.
    """
    with cursor() as conn:
        games = load_frame(conn, provider)
        history = conn.execute(
            """
            SELECT game_id, team, kickoff, season, fp_margin_pts
            FROM team_game
            WHERE fp_margin_pts IS NOT NULL
            ORDER BY kickoff
            """
        ).df()

    if games.empty or history.empty:
        return {"error": "Need both lines and features loaded."}

    history["kickoff"] = pd.to_datetime(history["kickoff"], utc=True)
    history = history.sort_values("kickoff")
    # Season-to-date mean, shifted so a game never sees itself. This is the
    # same point-in-time discipline the backtester enforces; without the shift
    # the regression would be predicting a game from its own field position and
    # would look spectacular.
    history["prior_fp"] = (
        history.groupby(["season", "team"])["fp_margin_pts"]
        .transform(lambda s: s.shift(1).expanding().mean())
    )

    prior = history[["game_id", "team", "prior_fp"]]
    frame = games.merge(
        prior.rename(columns={"team": "home_team", "prior_fp": "home_prior_fp"}),
        on=["game_id", "home_team"],
        how="left",
    ).merge(
        prior.rename(columns={"team": "away_team", "prior_fp": "away_prior_fp"}),
        on=["game_id", "away_team"],
        how="left",
    )
    frame = frame.dropna(
        subset=["market_margin", "margin", "home_prior_fp", "away_prior_fp"]
    )
    if len(frame) < 200:
        return {"error": f"Only {len(frame)} usable games; need at least 200."}

    edge = (frame["home_prior_fp"] - frame["away_prior_fp"]).to_numpy(float)
    residual = (frame["margin"] - frame["market_margin"]).to_numpy(float)
    fit = _wls(np.column_stack([np.ones_like(edge), edge]), residual)
    beta, t = float(fit["beta"][1]), float(fit["t"][1])

    if abs(t) < 2:
        verdict = (
            "The market has this priced. Prior field-position edge does not "
            "predict which way the line misses, so hidden yardage as measured "
            "here is real information about the sport but not about the odds."
        )
    elif beta > 0:
        verdict = (
            f"Teams with a prior field-position edge beat the closing line by "
            f"about {beta:.2f} points per point of edge. The market is "
            "underrating it, which is exactly the exploitable case."
        )
    else:
        verdict = (
            f"Teams with a prior field-position edge fall {abs(beta):.2f} points "
            "short of the closing line per point of edge — the market "
            "over-extrapolates it. Still tradeable, but in the opposite "
            "direction to the obvious one."
        )

    return {
        "coefficient": round(beta, 4),
        "se": round(float(fit["se"][1]), 4),
        "t": round(t, 2),
        "games": fit["n"],
        "interpretation": verdict,
    }


def fp_curve_table() -> pd.DataFrame:
    """The fitted expected-points-by-field-position curve, for display."""
    with cursor() as conn:
        return conn.execute(
            """
            SELECT yards_to_goal_bin, expected_points, drives
            FROM fp_curve WHERE fit_key = 'global'
            ORDER BY yards_to_goal_bin
            """
        ).df()
