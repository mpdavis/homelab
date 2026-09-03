"""Run a hypothesis through the funnel, recording every look.

Order matters and is not arbitrary. Persistence and the market test are one
scan each and kill most ideas; the backtest is the expensive stage and, more to
the point, the stage that *consumes the sample*. Every idea that reaches it
raises the significance bar for every idea after it, so the cheap filters exist
as much to protect the denominator as to save time.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..analysis import _wls
from ..backtest import BacktestConfig, load_frame, run_backtest
from ..db import cursor
from . import registry, sqlguard

log = logging.getLogger(__name__)

# A metric has to repeat to be a property of a team rather than of a Saturday.
# Two conditions, because either alone is wrong: the correlation has to be big
# enough to matter, AND distinguishable from zero given how many team-seasons
# it was measured over. A bare threshold on r ignores the second — over 48
# team-seasons the null standard deviation of r is about 0.15, so "r > 0.15"
# is a coin flip dressed as a criterion.
MIN_SPLIT_HALF_R = 0.15
MIN_SPLIT_HALF_Z = 2.0
# ...and the market has to have missed it. |t| here is uncorrected on purpose:
# this is a filter, not a finding, and a strict bar would throw away ideas
# before the stage that can actually judge them.
MIN_MARKET_T = 1.5
MIN_GAMES = 400


def evaluate(
    hypothesis: registry.Hypothesis,
    *,
    stage_limit: str = "backtest",
    model: str = "decomposed",
    edge_threshold: float = 2.0,
) -> dict:
    """Run the funnel. Returns a report; every stage is recorded either way."""
    with cursor() as conn:
        seasons = registry.search_seasons(conn)
        registry.guard_seasons(conn, seasons, stage="persistence")
        frame, metrics = sqlguard.run(conn, hypothesis.sql, seasons=seasons)
        market = load_frame(conn)

    metric = metrics[0]
    report: dict = {
        "hypothesis_id": hypothesis.hypothesis_id,
        "name": hypothesis.name,
        "metric": metric,
        "seasons": seasons,
        "stages": {},
    }

    # --- 1. is it a skill? --------------------------------------------------
    persistence = _split_half(conn_frame=frame, metric=metric, market=market)
    passed = (
        persistence["split_half_r"] >= MIN_SPLIT_HALF_R
        and persistence.get("z", 0.0) >= MIN_SPLIT_HALF_Z
    )
    registry.record_trial(
        hypothesis.hypothesis_id, "persistence", seasons=seasons,
        passed=passed, statistic=persistence["split_half_r"], metrics=persistence,
    )
    report["stages"]["persistence"] = {**persistence, "passed": passed}
    if not passed:
        # "Cannot tell" and "measured, and it does not repeat" are different
        # findings, and the ledger keeps this string forever. Recording the
        # second when the first is true would retire an idea that was never
        # actually tested.
        if persistence.get("note"):
            registry.set_status(hypothesis.hypothesis_id, "proposed")
            report["outcome"] = (
                f"not judged: {persistence['note']} "
                f"({persistence['team_seasons']} team-seasons). Ingest more "
                "history and re-run — this is not a verdict on the idea."
            )
        else:
            registry.set_status(hypothesis.hypothesis_id, "rejected")
            report["outcome"] = (
                f"rejected: {metric} does not repeat (split-half r="
                f"{persistence['split_half_r']:+.3f}, z={persistence.get('z', 0):+.2f} "
                f"over {persistence['team_seasons']} team-seasons), so it measures "
                "what happened to a team rather than a property of one."
            )
        return report

    # --- 2. does the market already price it? -------------------------------
    priced = _market_test(frame, metric, market)
    passed = abs(priced.get("t", 0.0)) >= MIN_MARKET_T and priced["games"] >= MIN_GAMES
    registry.record_trial(
        hypothesis.hypothesis_id, "market", seasons=seasons,
        passed=passed, statistic=priced.get("t"), metrics=priced,
    )
    report["stages"]["market"] = {**priced, "passed": passed}
    if not passed:
        if priced.get("note") or priced["games"] < MIN_GAMES:
            # Same distinction as the gate above: too few priced games is not a
            # finding that the market prices it.
            registry.set_status(hypothesis.hypothesis_id, "proposed")
            report["outcome"] = (
                f"not judged: only {priced['games']:,} games carry both this "
                f"metric and a market line, below the {MIN_GAMES:,} needed. "
                "Not a verdict on the idea."
            )
        else:
            registry.set_status(hypothesis.hypothesis_id, "rejected")
            report["outcome"] = (
                f"rejected: the market has it priced (t={priced.get('t', 0):+.2f} "
                f"on {priced['games']:,} games). Real football information, but "
                "not information about the odds."
            )
        return report

    if stage_limit == "market":
        report["outcome"] = "passed the cheap filters; stopped before the backtest"
        return report

    # --- 3. does it survive out of sample, given how often we have looked? --
    config = BacktestConfig(
        model=model,
        first_season=min(seasons),
        last_season=max(seasons),
        edge_threshold=edge_threshold,
        label=f"research:{hypothesis.name}",
    )
    result = run_backtest(config, persist=True)
    profits = result["bets"]
    profits = profits[profits["side"] != "pass"]["profit_units"].to_numpy(float)
    t_stat = _t_of_mean(profits)
    verdict = registry.assess(t_stat)

    registry.record_trial(
        hypothesis.hypothesis_id, "backtest", seasons=seasons,
        passed=verdict["clears_corrected_bar"], statistic=t_stat,
        metrics={**result["metrics"], "correction": verdict},
        note=result["run_id"],
    )
    report["stages"]["backtest"] = {
        "run_id": result["run_id"],
        "bets": result["metrics"]["bets"],
        "roi": result["metrics"]["roi"],
        "t": round(t_stat, 3),
        "correction": verdict,
        "passed": verdict["clears_corrected_bar"],
    }

    if verdict["clears_corrected_bar"]:
        registry.set_status(hypothesis.hypothesis_id, "survived")
        report["outcome"] = (
            f"survived the search set. {verdict['verdict']}. It has NOT seen the "
            "holdout — that is one shot, via `gridiron research holdout`."
        )
    else:
        registry.set_status(hypothesis.hypothesis_id, "rejected")
        report["outcome"] = f"rejected: {verdict['verdict']}"
    return report


def _t_of_mean(profits: np.ndarray) -> float:
    """t on mean profit per bet. Zero for an empty or degenerate sample."""
    if profits.size < 2:
        return 0.0
    sd = float(profits.std(ddof=1))
    if sd <= 0:
        return 0.0
    return float(profits.mean() / (sd / np.sqrt(profits.size)))


def _split_half(*, conn_frame: pd.DataFrame, metric: str, market: pd.DataFrame) -> dict:
    """Odd games against even games, per team-season, Spearman-Brown corrected."""
    joined = conn_frame.merge(
        market[["game_id", "season", "kickoff"]], on="game_id", how="inner"
    ).dropna(subset=[metric, "kickoff"])
    if joined.empty:
        return {"split_half_r": 0.0, "team_seasons": 0, "note": "no rows after join"}

    joined = joined.sort_values("kickoff")
    joined["n"] = joined.groupby(["season", "team"]).cumcount()
    joined["half"] = np.where(joined["n"] % 2 == 0, "even", "odd")
    pivot = joined.pivot_table(
        index=["season", "team"], columns="half", values=metric, aggfunc="mean"
    ).dropna()
    if len(pivot) < 30 or {"odd", "even"} - set(pivot.columns):
        return {
            "split_half_r": 0.0,
            "team_seasons": int(len(pivot)),
            "note": "too few team-seasons to judge repeatability",
        }
    r = float(pivot["odd"].corr(pivot["even"]))
    if not np.isfinite(r):
        r = 0.0
    adjusted = (2 * r) / (1 + r) if r > -1 else float("nan")
    # Fisher's z: atanh(r) * sqrt(n-3) is standard normal under the null, which
    # is what turns "r looks big" into "r is bigger than this sample could
    # produce by chance".
    n = len(pivot)
    z = 0.0
    if n > 3 and abs(r) < 1.0:
        z = float(np.arctanh(r) * np.sqrt(n - 3))
    return {
        "split_half_r": round(r, 4),
        "full_season_r": round(float(adjusted), 4),
        "z": round(z, 3),
        "team_seasons": int(n),
    }


def _market_test(frame: pd.DataFrame, metric: str, market: pd.DataFrame) -> dict:
    """Regress the market's error on the two sides' prior average of the metric.

    Prior, strictly: a shifted expanding mean within the season, so a game never
    contributes to the number used to predict it. Without the shift this
    regression predicts a game from its own result and looks spectacular.
    """
    history = frame.merge(
        market[["game_id", "season", "kickoff"]], on="game_id", how="inner"
    ).dropna(subset=[metric, "kickoff"]).sort_values("kickoff")
    if history.empty:
        return {"t": 0.0, "games": 0, "note": "no rows after join"}

    history["prior"] = history.groupby(["season", "team"])[metric].transform(
        lambda s: s.shift(1).expanding().mean()
    )
    prior = history[["game_id", "team", "prior"]]

    merged = market.merge(
        prior.rename(columns={"team": "home_team", "prior": "home_prior"}),
        on=["game_id", "home_team"], how="left",
    ).merge(
        prior.rename(columns={"team": "away_team", "prior": "away_prior"}),
        on=["game_id", "away_team"], how="left",
    ).dropna(subset=["market_margin", "margin", "home_prior", "away_prior"])

    if len(merged) < MIN_GAMES:
        return {"t": 0.0, "games": int(len(merged)), "note": "too few priced games"}

    edge = (merged["home_prior"] - merged["away_prior"]).to_numpy(float)
    if float(np.std(edge)) == 0.0:
        return {"t": 0.0, "games": int(len(merged)), "note": "metric has no spread"}
    residual = (merged["margin"] - merged["market_margin"]).to_numpy(float)
    fit = _wls(np.column_stack([np.ones_like(edge), edge]), residual)
    return {
        "coefficient": round(float(fit["beta"][1]), 4),
        "se": round(float(fit["se"][1]), 4),
        "t": round(float(fit["t"][1]), 3),
        "games": int(fit["n"]),
    }


def holdout(hypothesis_id: str, *, model: str = "decomposed",
            edge_threshold: float = 2.0) -> dict:
    """Spend the holdout on one finalist. Irreversible by design."""
    with cursor() as conn:
        row = conn.execute(
            "SELECT name, status FROM research_hypotheses WHERE hypothesis_id = ?",
            [hypothesis_id],
        ).fetchone()
        seasons = registry.holdout_seasons(conn)
    if row is None:
        raise ValueError(f"no hypothesis {hypothesis_id!r}")
    name, status = row
    if status == "spent":
        raise ValueError(
            f"{name!r} has already had its look at the holdout. A second one is "
            "not a second independent test — it is the search continuing on data "
            "that was supposed to be untouched."
        )
    if status != "survived":
        raise ValueError(
            f"{name!r} is {status!r}, not 'survived'. Only a candidate that "
            "cleared the corrected bar on the search set earns the holdout."
        )

    config = BacktestConfig(
        model=model,
        first_season=min(seasons),
        last_season=max(seasons),
        edge_threshold=edge_threshold,
        label=f"holdout:{name}",
    )
    result = run_backtest(config, persist=True)
    bets = result["bets"]
    profits = bets[bets["side"] != "pass"]["profit_units"].to_numpy(float)
    t_stat = _t_of_mean(profits)

    # One hypothesis, one look: the correction here is n=1, which is the whole
    # reason the holdout was kept clean.
    verdict = registry.assess(t_stat, n_trials=1)
    registry.record_trial(
        hypothesis_id, "holdout", seasons=seasons, passed=verdict["clears_corrected_bar"],
        statistic=t_stat, metrics={**result["metrics"], "correction": verdict},
        note=result["run_id"],
    )
    registry.set_status(hypothesis_id, "spent")
    return {
        "name": name,
        "seasons": seasons,
        "run_id": result["run_id"],
        "bets": result["metrics"]["bets"],
        "roi": result["metrics"]["roi"],
        "roi_ci": [result["metrics"]["roi_ci_low"], result["metrics"]["roi_ci_high"]],
        "t": round(t_stat, 3),
        "correction": verdict,
        "outcome": (
            "held up out of sample" if verdict["clears_corrected_bar"]
            else "did not hold up out of sample"
        ),
    }
