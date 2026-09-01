"""Walk-forward backtesting.

The engine steps through history one week at a time. At each step it refits the
model on everything that had finished before that week's first kickoff,
predicts that week's games, compares the predictions with the book's number,
and records what a flat bet would have done.

WHY IT REFITS EVERY WEEK. Fitting once and predicting the whole sample is
faster and completely worthless: the model would be using November to predict
September. Refitting weekly is the cheapest schedule that never does that. It
is also why a ten-season backtest takes a couple of minutes rather than a
couple of seconds — that is the cost of the result meaning something.

WHAT TO BELIEVE IN THE OUTPUT. Not the win rate. A season is about 800 bettable
games and a strategy might bet 150 of them; at that sample a 55% strategy and a
50% strategy are separated by less than the noise. The three numbers that carry
information are the bootstrap confidence interval on ROI, the shape of the
sweep across half-lives (a real effect is a smooth hill, an artefact is a
single spike), and the model's mean absolute error against the market's. The
headline win rate is reported because you will look for it, with its interval
attached because you should not read it alone.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .config import settings
from .db import cursor
from .features.prestige import prestige_gap, team_season_prestige
from .models import build_model
from .models.ratings import MIN_TRAIN_GAMES
from .models.weighting import Sweep, Weighting

log = logging.getLogger(__name__)

# A standard -110 both ways. Winning 100 units risks 110, so a bet returns
# 0.909 units and breaks even at 52.38%.
DEFAULT_PRICE = -110

# CFBD's merged line across books. It is the most complete series by a distance
# — individual books have gaps in older seasons that would silently shrink the
# sample and bias it toward the games books cared about.
DEFAULT_PROVIDER = "consensus"


def american_to_profit(price: int) -> float:
    """Units returned on a one-unit winning bet."""
    return price / 100.0 if price > 0 else 100.0 / abs(price)


def breakeven_rate(price: int) -> float:
    profit = american_to_profit(price)
    return 1.0 / (1.0 + profit)


@dataclass
class BacktestConfig:
    model: str = "decomposed"
    params: dict = field(default_factory=dict)
    first_season: int | None = None
    last_season: int | None = None
    provider: str = DEFAULT_PROVIDER
    edge_threshold: float = 2.5
    price: int = DEFAULT_PRICE
    # "close" grades against the closing number, which is the hardest line to
    # beat and therefore the honest test. "open" simulates betting the opening
    # number and additionally reports closing-line value.
    bet_line: str = "close"
    min_train_games: int = MIN_TRAIN_GAMES
    label: str = ""


def load_frame(conn, provider: str = DEFAULT_PROVIDER) -> pd.DataFrame:
    """One row per completed game, home perspective, with everything attached.

    Loads *all* seasons regardless of which ones are being tested: early
    seasons are the burn-in that the first tested week trains on. Restricting
    the load to the test seasons would leave week one of the sample with
    nothing to learn from.
    """
    games = conn.execute(
        """
        SELECT
            tg.game_id, tg.season, tg.week, tg.kickoff,
            tg.team      AS home_team,
            tg.opponent  AS away_team,
            tg.neutral_site,
            tg.margin,
            tg.efficiency_margin,
            tg.fp_margin_pts,
            tg.points          AS home_points,
            tg.points_allowed  AS away_points,
            g.home_classification,
            g.away_classification,
            l.spread,
            l.spread_open
        FROM team_game tg
        JOIN games g ON g.game_id = tg.game_id
        LEFT JOIN lines l ON l.game_id = tg.game_id AND l.provider = ?
        WHERE tg.is_home
        ORDER BY tg.kickoff
        """,
        [provider],
    ).df()

    if games.empty:
        return games

    # The one sign conversion in the package. A book quotes the home team's
    # spread with the favourite negative; the margin the market expects is its
    # negation. See the package docstring.
    games["market_margin"] = -games["spread"]
    games["market_margin_open"] = -games["spread_open"]
    games["kickoff"] = pd.to_datetime(games["kickoff"], utc=True)

    prestige = team_season_prestige(conn)
    if prestige.empty:
        games["prestige_gap"] = 0.0
        games["home_prestige"] = 0.0
        games["away_prestige"] = 0.0
    else:
        games = prestige_gap(prestige, games)
    return games


def fbs_teams(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT school FROM teams WHERE lower(coalesce(classification, 'fbs')) = 'fbs'"
        ).fetchall()
    }


def run_backtest(config: BacktestConfig, *, persist: bool = True) -> dict:
    """Walk forward through the test seasons. Returns metrics plus the bets."""
    with cursor() as conn:
        frame = load_frame(conn, config.provider)
        fbs = fbs_teams(conn)

    if frame.empty:
        raise ValueError(
            "No completed games with features. Run `gridiron ingest` then "
            "`gridiron features build` first."
        )

    first = config.first_season or int(frame["season"].min())
    last = config.last_season or int(frame["season"].max())

    line_column = (
        "market_margin_open" if config.bet_line == "open" else "market_margin"
    )
    if line_column not in frame.columns:
        raise ValueError(f"No {config.bet_line} line available")

    records: list[dict] = []
    weeks = (
        frame[(frame["season"] >= first) & (frame["season"] <= last)]
        .dropna(subset=["kickoff"])
        .groupby(["season", "week"], dropna=True)
    )

    skipped_weeks = 0
    for (season, week), fixtures in weeks:
        as_of = fixtures["kickoff"].min()
        history = frame[frame["kickoff"] < as_of]
        if len(history) < config.min_train_games:
            skipped_weeks += 1
            continue

        model = build_model(config.model, **config.params)
        # Pooling non-FBS opponents is a property of the data, not of the
        # model, so it is injected rather than configured per model.
        model.fbs = fbs
        model.fit(history, as_of)
        predictions = model.predict(fixtures)

        for prediction, row in zip(predictions, fixtures.itertuples(index=False)):
            market = getattr(row, line_column)
            if not np.isfinite(prediction) or pd.isna(market):
                continue
            records.append(
                {
                    "game_id": int(row.game_id),
                    "season": int(season),
                    "week": int(week),
                    "kickoff": row.kickoff,
                    "home_team": row.home_team,
                    "away_team": row.away_team,
                    "model_margin": float(prediction),
                    "market_margin": float(market),
                    "market_margin_close": _maybe_float(row.market_margin),
                    "actual_margin": float(row.margin),
                }
            )

    if not records:
        raise ValueError(
            "No gradeable games. Most likely the seasons requested have no "
            f"lines from provider {config.provider!r}, or the training window "
            f"never reached {config.min_train_games} games."
        )

    bets = pd.DataFrame.from_records(records)
    bets = _grade(bets, config)
    metrics = summarise(bets, config)
    metrics["skipped_weeks"] = skipped_weeks

    run_id = uuid.uuid4().hex[:12]
    if persist:
        _persist(run_id, config, bets, metrics)
    metrics["run_id"] = run_id
    return {"run_id": run_id, "metrics": metrics, "bets": bets}


def _maybe_float(value) -> float | None:
    return None if pd.isna(value) else float(value)


def _grade(bets: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    """Decide each bet and score it against the result."""
    bets = bets.copy()
    bets["edge"] = bets["model_margin"] - bets["market_margin"]

    threshold = config.edge_threshold
    bets["side"] = np.select(
        [bets["edge"] >= threshold, bets["edge"] <= -threshold],
        ["home", "away"],
        default="pass",
    )

    # Against the spread, the home side wins when the actual margin clears the
    # number the market set. Exactly on it is a push, which happens often
    # enough on whole-number spreads to matter — treating pushes as losses
    # would cost about a point of measured win rate.
    cover = bets["actual_margin"] - bets["market_margin"]
    profit = american_to_profit(config.price)
    bets["result"] = np.select(
        [
            bets["side"] == "pass",
            cover == 0,
            (bets["side"] == "home") & (cover > 0),
            (bets["side"] == "away") & (cover < 0),
        ],
        ["pass", "push", "win", "win"],
        default="loss",
    )
    bets["profit_units"] = np.select(
        [bets["result"] == "win", bets["result"] == "loss"],
        [profit, -1.0],
        default=0.0,
    )

    # Closing-line value only means something when the bet was placed at some
    # other number. Betting the close and then measuring value against the
    # close would report zero by construction.
    if config.bet_line == "open" and "market_margin_close" in bets.columns:
        direction = np.select(
            [bets["side"] == "home", bets["side"] == "away"], [1.0, -1.0], default=0.0
        )
        bets["clv_pts"] = direction * (
            bets["market_margin_close"] - bets["market_margin"]
        )
    else:
        bets["clv_pts"] = np.nan
    return bets


def summarise(bets: pd.DataFrame, config: BacktestConfig) -> dict:
    """Turn graded bets into the numbers worth reading."""
    placed = bets[bets["side"] != "pass"]
    decided = placed[placed["result"].isin(["win", "loss"])]
    wins = int((decided["result"] == "win").sum())
    losses = int((decided["result"] == "loss").sum())
    pushes = int((placed["result"] == "push").sum())
    staked = len(decided)

    profit_units = float(placed["profit_units"].sum())
    roi = profit_units / staked if staked else 0.0
    win_rate = wins / staked if staked else 0.0

    low, high = _bootstrap_roi(placed["profit_units"].to_numpy())

    metrics = {
        "model": config.model,
        "params": config.params,
        "provider": config.provider,
        "bet_line": config.bet_line,
        "edge_threshold": config.edge_threshold,
        "price": config.price,
        "games_evaluated": int(len(bets)),
        "bets": staked,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": round(win_rate, 4),
        "breakeven_rate": round(breakeven_rate(config.price), 4),
        "profit_units": round(profit_units, 2),
        "roi": round(roi, 4),
        "roi_ci_low": round(low, 4),
        "roi_ci_high": round(high, 4),
        # The honest headline. A confidence interval straddling zero means the
        # sample cannot tell this strategy from a coin flip, whatever the win
        # rate says.
        "beats_breakeven": bool(low > 0),
        "mae_model": _mae(bets["model_margin"], bets["actual_margin"]),
        "mae_market": _mae(bets["market_margin"], bets["actual_margin"]),
        "bias_model": round(
            float((bets["model_margin"] - bets["actual_margin"]).mean()), 3
        ),
        "mean_abs_edge": round(float(bets["edge"].abs().mean()), 3),
    }
    if placed["clv_pts"].notna().any():
        metrics["clv_pts_mean"] = round(float(placed["clv_pts"].mean()), 3)
        metrics["clv_positive_rate"] = round(
            float((placed["clv_pts"] > 0).mean()), 4
        )

    metrics["by_season"] = _by(placed, "season", config)
    metrics["by_edge_bucket"] = _calibration(bets, config)
    return metrics


def _mae(predicted: pd.Series, actual: pd.Series) -> float | None:
    mask = predicted.notna() & actual.notna()
    if not mask.any():
        return None
    return round(float((predicted[mask] - actual[mask]).abs().mean()), 3)


def _by(placed: pd.DataFrame, column: str, config: BacktestConfig) -> list[dict]:
    out = []
    for key, group in placed.groupby(column):
        decided = group[group["result"].isin(["win", "loss"])]
        staked = len(decided)
        out.append(
            {
                column: int(key),
                "bets": staked,
                "wins": int((decided["result"] == "win").sum()),
                "win_rate": round(
                    float((decided["result"] == "win").mean()) if staked else 0.0, 4
                ),
                "profit_units": round(float(group["profit_units"].sum()), 2),
                "roi": round(
                    float(group["profit_units"].sum() / staked) if staked else 0.0, 4
                ),
            }
        )
    return out


def _calibration(bets: pd.DataFrame, config: BacktestConfig) -> list[dict]:
    """Win rate by size of edge — the single most diagnostic table here.

    A model that has found something real wins more when it disagrees with the
    market more. If the 8-point-edge bucket does no better than the 2-point
    bucket, the "edges" are noise, and no amount of headline ROI should
    persuade you otherwise: it means the profitable bets were not the ones the
    model was most confident about.
    """
    edges = bets["edge"].abs()
    buckets = pd.cut(
        edges,
        bins=[0, 1, 2, 3, 5, 7, 10, np.inf],
        labels=["0-1", "1-2", "2-3", "3-5", "5-7", "7-10", "10+"],
        right=False,
    )
    profit = american_to_profit(config.price)
    out = []
    for label, group in bets.groupby(buckets, observed=True):
        cover = group["actual_margin"] - group["market_margin"]
        direction = np.where(group["edge"] >= 0, 1.0, -1.0)
        signed = direction * cover
        decided = signed[signed != 0]
        if len(decided) == 0:
            continue
        win_rate = float((decided > 0).mean())
        out.append(
            {
                "edge_bucket": str(label),
                "games": int(len(group)),
                "decided": int(len(decided)),
                "win_rate": round(win_rate, 4),
                # What flat-betting every game in this bucket would return, so
                # the bucket can be read as a strategy rather than as a stat.
                "roi": round(win_rate * profit - (1 - win_rate), 4),
            }
        )
    return out


def _bootstrap_roi(
    profits: np.ndarray, iterations: int = 2000, seed: int = 20260831
) -> tuple[float, float]:
    """A 95% percentile interval for ROI, by resampling the bets.

    Bets are treated as independent, which they are not quite — several bets in
    one week share a model fit, and correlated bets make the true interval
    slightly wider than this. Read a marginal result as worse than it looks
    rather than better.
    """
    if len(profits) < 20:
        return (float("-inf"), float("inf"))
    rng = np.random.default_rng(seed)
    draws = rng.choice(profits, size=(iterations, len(profits)), replace=True)
    rois = draws.mean(axis=1)
    return float(np.percentile(rois, 2.5)), float(np.percentile(rois, 97.5))


def _persist(run_id: str, config: BacktestConfig, bets: pd.DataFrame, metrics: dict):
    with cursor() as conn:
        conn.execute(
            """
            INSERT INTO backtest_runs
                (run_id, created_at, label, model, params, first_season,
                 last_season, metrics)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                datetime.now(timezone.utc),
                config.label or f"{config.model} @ {config.edge_threshold}",
                config.model,
                json.dumps(config.params, default=str),
                int(bets["season"].min()),
                int(bets["season"].max()),
                json.dumps(metrics, default=str),
            ],
        )
        columns = [
            "run_id", "game_id", "season", "week", "kickoff", "home_team",
            "away_team", "model_margin", "market_margin", "actual_margin",
            "edge", "side", "result", "profit_units",
        ]
        stored = bets.copy()
        stored["run_id"] = run_id
        conn.register("_bets", stored[columns])
        try:
            conn.execute(
                f"INSERT INTO backtest_bets ({', '.join(columns)}) "
                f"SELECT {', '.join(columns)} FROM _bets"
            )
        finally:
            conn.unregister("_bets")


# ---------------------------------------------------------------------------
# The sweep — "varying amounts of time", made into a table
# ---------------------------------------------------------------------------


def run_sweep(
    config: BacktestConfig,
    sweep: Sweep | None = None,
    *,
    persist: bool = False,
) -> pd.DataFrame:
    """Backtest the same model across a ladder of recency settings.

    This is the answer to "how much history should count", and it is meant to
    be read as a shape rather than as a maximum. If ROI rises smoothly to a
    peak around a 200-day half-life and falls away either side, that is a
    finding. If one half-life in the ladder is wildly profitable and its
    neighbours are not, that is overfitting to the sample and picking it will
    lose money.

    Each row is a full walk-forward backtest, so this is slow by design.
    """
    sweep = sweep or Sweep()
    rows = []
    for weighting in sweep.weightings():
        params = dict(config.params)
        params["weighting_kind"] = weighting.kind
        params["half_life_days"] = weighting.half_life_days
        params["window_days"] = weighting.window_days
        params["season_carryover"] = weighting.season_carryover

        variant = BacktestConfig(**{**asdict(config), "params": params})
        variant.label = f"{config.model} / {weighting.describe()}"
        try:
            result = run_backtest(variant, persist=persist)
        except ValueError as exc:
            log.warning("Sweep point %s failed: %s", weighting.describe(), exc)
            continue
        metrics = result["metrics"]
        rows.append(
            {
                "weighting": weighting.describe(),
                "kind": weighting.kind,
                "half_life_days": weighting.half_life_days
                if weighting.kind == "exponential"
                else None,
                "bets": metrics["bets"],
                "win_rate": metrics["win_rate"],
                "roi": metrics["roi"],
                "roi_ci_low": metrics["roi_ci_low"],
                "roi_ci_high": metrics["roi_ci_high"],
                "profit_units": metrics["profit_units"],
                "mae_model": metrics["mae_model"],
                "mae_market": metrics["mae_market"],
                "run_id": result["run_id"] if persist else None,
            }
        )
    return pd.DataFrame(rows)


def default_config() -> BacktestConfig:
    cfg = settings()
    return BacktestConfig(
        model=cfg.default_model,
        params={
            "half_life_days": cfg.default_half_life_days,
            "ridge_lambda": cfg.default_ridge_lambda,
        },
        edge_threshold=cfg.default_edge_threshold,
    )


def list_runs(limit: int = 50) -> pd.DataFrame:
    with cursor() as conn:
        return conn.execute(
            """
            SELECT run_id, created_at, label, model, first_season, last_season,
                   metrics
            FROM backtest_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [limit],
        ).df()
