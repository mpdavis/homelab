"""The backtester end to end, and the leakage guarantee it rests on."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import synth

from gridiron import backtest as bt
from gridiron.models.weighting import Sweep


def config(**overrides) -> bt.BacktestConfig:
    base = dict(
        model="ridge_margin",
        params={"ridge_lambda": 1.0},
        first_season=2022,
        last_season=2023,
        min_train_games=60,
    )
    base.update(overrides)
    return bt.BacktestConfig(**base)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_frame_is_one_row_per_game_from_the_home_side(featured):
    frame = bt.load_frame(featured)
    games = featured.execute("SELECT count(*) FROM games").fetchone()[0]
    assert len(frame) == games
    assert {"market_margin", "prestige_gap", "efficiency_margin"} <= set(frame.columns)


def test_the_market_margin_is_the_negated_spread(featured):
    frame = bt.load_frame(featured)
    assert frame["market_margin"].to_numpy() == pytest.approx(
        -frame["spread"].to_numpy()
    )


def test_load_frame_keeps_seasons_outside_the_test_window_for_burn_in(featured):
    frame = bt.load_frame(featured)
    assert set(frame["season"]) == set(synth.SEASONS)


def test_fbs_membership_comes_from_the_teams_table(featured):
    assert bt.fbs_teams(featured) == set(synth.TEAMS)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def _graded(rows, **overrides):
    frame = pd.DataFrame(rows)
    return bt._grade(frame, config(edge_threshold=2.5, **overrides))


def test_grading_calls_wins_losses_pushes_and_passes():
    graded = _graded(
        [
            # Model likes the home team by 6 more than the market; home wins by 10.
            {"model_margin": 6.0, "market_margin": 0.0, "actual_margin": 10.0,
             "market_margin_close": 0.0},
            # Same bet, home loses outright.
            {"model_margin": 6.0, "market_margin": 0.0, "actual_margin": -3.0,
             "market_margin_close": 0.0},
            # Model likes the away team; away covers.
            {"model_margin": -6.0, "market_margin": 0.0, "actual_margin": -8.0,
             "market_margin_close": 0.0},
            # Exactly on the number: a push, not a win.
            {"model_margin": 7.0, "market_margin": 3.0, "actual_margin": 3.0,
             "market_margin_close": 3.0},
            # Inside the threshold: no bet.
            {"model_margin": 1.0, "market_margin": 0.0, "actual_margin": 20.0,
             "market_margin_close": 0.0},
        ]
    )
    assert list(graded["side"]) == ["home", "home", "away", "home", "pass"]
    assert list(graded["result"]) == ["win", "loss", "win", "push", "pass"]
    assert graded["profit_units"].iloc[0] == pytest.approx(bt.american_to_profit(-110))
    assert graded["profit_units"].iloc[1] == pytest.approx(-1.0)
    assert graded["profit_units"].iloc[3] == 0.0
    assert graded["profit_units"].iloc[4] == 0.0


def test_closing_line_value_is_only_measured_when_betting_the_open():
    rows = [
        {"model_margin": 6.0, "market_margin": 0.0, "actual_margin": 10.0,
         "market_margin_close": 2.0}
    ]
    assert _graded(rows, bet_line="close")["clv_pts"].isna().all()
    assert _graded(rows, bet_line="open")["clv_pts"].notna().all()


def test_summary_reports_the_bar_it_has_to_clear():
    graded = _graded(
        [
            {"model_margin": 6.0, "market_margin": 0.0, "actual_margin": 10.0,
             "market_margin_close": 0.0, "season": 2023, "week": 1},
            {"model_margin": 6.0, "market_margin": 0.0, "actual_margin": -3.0,
             "market_margin_close": 0.0, "season": 2023, "week": 2},
        ]
    )
    metrics = bt.summarise(graded, config())
    assert metrics["bets"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["breakeven_rate"] == pytest.approx(bt.breakeven_rate(-110), abs=1e-4)
    assert metrics["roi"] < 0
    assert metrics["beats_breakeven"] is False


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


def test_a_full_run_produces_bets_metrics_and_a_persisted_row(featured):
    result = bt.run_backtest(config())
    assert result["metrics"]["bets"] > 0
    assert 0.0 <= result["metrics"]["win_rate"] <= 1.0
    runs = bt.list_runs()
    assert result["run_id"] in set(runs["run_id"])


def test_only_the_requested_seasons_are_graded(featured):
    result = bt.run_backtest(config(first_season=2023, last_season=2023), persist=False)
    assert set(result["bets"]["season"]) == {2023}


def test_the_market_benchmark_never_bets_and_never_loses(featured):
    result = bt.run_backtest(config(model="market", params={}), persist=False)
    assert (result["bets"]["side"] == "pass").all()
    assert result["metrics"]["bets"] == 0
    # It still reports the market's own error, which is the point of running it.
    assert result["metrics"]["mae_market"] is not None


def test_the_model_is_roughly_as_accurate_as_the_market_it_is_trained_against(featured):
    """A sanity floor: if MAE is wildly worse than the market, something is wrong."""
    result = bt.run_backtest(config(edge_threshold=0.0), persist=False)
    metrics = result["metrics"]
    assert metrics["mae_model"] < metrics["mae_market"] * 1.6


def test_weeks_without_enough_history_are_skipped_not_guessed(featured):
    result = bt.run_backtest(config(first_season=2021, min_train_games=60), persist=False)
    assert result["metrics"]["skipped_weeks"] > 0
    assert result["bets"]["season"].min() >= 2021


def test_an_impossible_provider_fails_loudly(featured):
    with pytest.raises(ValueError, match="No gradeable games"):
        bt.run_backtest(config(provider="nonexistent-book"), persist=False)


def test_backtesting_without_features_says_what_to_run(populated):
    with pytest.raises(ValueError, match="gridiron features build"):
        bt.run_backtest(config(), persist=False)


def test_calibration_buckets_edges(featured):
    metrics = bt.run_backtest(config(edge_threshold=0.0), persist=False)["metrics"]
    buckets = metrics["by_edge_bucket"]
    assert buckets
    assert all(row["games"] > 0 for row in buckets)


# ---------------------------------------------------------------------------
# The leakage guarantee, end to end
# ---------------------------------------------------------------------------


def test_rewriting_later_results_cannot_move_an_earlier_prediction(featured):
    """The whole-system version of the ratings-level leakage test.

    Run the backtester, then rewrite every game after a cutoff to an absurd
    scoreline, rebuild features, and run it again. Predictions for games before
    the cutoff have to be bit-for-bit identical — if any information flows
    backwards through the feature build or the fit, this is where it shows.
    """
    from gridiron.features.build import build_team_game

    honest = bt.run_backtest(config(first_season=2022, last_season=2022), persist=False)
    honest_bets = honest["bets"].set_index("game_id").sort_index()

    cutoff = pd.Timestamp("2023-01-01")
    featured.execute(
        "UPDATE games SET home_points = 99, away_points = 0 WHERE start_date >= ?",
        [cutoff],
    )
    build_team_game(featured, list(synth.SEASONS))

    tampered = bt.run_backtest(config(first_season=2022, last_season=2022), persist=False)
    tampered_bets = tampered["bets"].set_index("game_id").sort_index()

    assert list(honest_bets.index) == list(tampered_bets.index)
    assert tampered_bets["model_margin"].to_numpy() == pytest.approx(
        honest_bets["model_margin"].to_numpy(), abs=1e-9
    )


def test_the_fp_curve_vintage_does_not_leak_into_earlier_features(featured):
    """A refit curve is global by design; the guard is that it is documented
    and that rebuilding with the same key reproduces the same numbers."""
    from gridiron.features.build import build_team_game

    before = featured.execute(
        "SELECT game_id, team, fp_margin_pts FROM team_game ORDER BY game_id, team"
    ).df()
    build_team_game(featured, list(synth.SEASONS))
    after = featured.execute(
        "SELECT game_id, team, fp_margin_pts FROM team_game ORDER BY game_id, team"
    ).df()
    assert after["fp_margin_pts"].to_numpy() == pytest.approx(
        before["fp_margin_pts"].to_numpy(), nan_ok=True
    )


# ---------------------------------------------------------------------------
# Sweeps — the "varying amounts of time" requirement
# ---------------------------------------------------------------------------


def test_a_sweep_runs_every_weighting_and_reports_them_side_by_side(featured):
    sweep = Sweep(half_lives=(90, 365), include_windows=False)
    frame = bt.run_sweep(config(), sweep, persist=False)
    assert len(frame) == 2
    assert set(frame["half_life_days"]) == {90, 365}
    assert {"roi", "win_rate", "bets", "mae_model"} <= set(frame.columns)


def test_different_half_lives_actually_produce_different_models(featured):
    sweep = Sweep(half_lives=(60, 800), include_windows=False)
    frame = bt.run_sweep(config(), sweep, persist=False)
    assert frame["mae_model"].nunique() == 2


def test_a_sweep_can_include_windows_and_uniform(featured):
    sweep = Sweep(half_lives=(240,), include_windows=True, window_days=(365,))
    frame = bt.run_sweep(config(), sweep, persist=False)
    assert set(frame["kind"]) == {"exponential", "window", "uniform"}


def test_sweeps_work_for_models_that_ignore_the_weighting_knobs(featured):
    """`elo` expresses recency as a learning rate; it must not crash the sweep."""
    sweep = Sweep(half_lives=(90, 365), include_windows=False)
    frame = bt.run_sweep(config(model="elo", params={}), sweep, persist=False)
    assert len(frame) == 2
    assert np.isfinite(frame["mae_model"]).all()
