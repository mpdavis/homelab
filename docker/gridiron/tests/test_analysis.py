"""The theory tests — the two hypotheses this whole service exists to check.

These assert two different kinds of thing, and it is worth keeping them apart:

* that a *planted* effect is recovered with the right sign and roughly the
  right size, which tests the arithmetic; and
* that a *thin* sample is refused rather than reported on, which tests the
  judgement. The second matters more. A regression that happily returns a
  coefficient from forty games is how a betting system talks someone into a
  losing strategy.
"""

from __future__ import annotations

import numpy as np
import pytest

import synth

from gridiron.analysis import (
    brand_premium,
    fp_curve_table,
    hidden_yardage_market_test,
    hidden_yardage_persistence,
    portal_and_prestige,
    _wls,
)


# ---------------------------------------------------------------------------
# The regression underneath everything
# ---------------------------------------------------------------------------


def test_wls_recovers_a_known_line():
    rng = np.random.default_rng(5)
    x = rng.normal(0, 1, 500)
    y = 3.0 - 2.0 * x + rng.normal(0, 0.5, 500)
    fit = _wls(np.column_stack([np.ones_like(x), x]), y)
    assert fit["beta"][0] == pytest.approx(3.0, abs=0.1)
    assert fit["beta"][1] == pytest.approx(-2.0, abs=0.1)
    assert fit["n"] == 500
    assert fit["r2"] > 0.9
    # A strong effect over 500 points should be many standard errors from zero.
    assert abs(fit["t"][1]) > 10


def test_wls_reports_a_weak_effect_as_weak():
    rng = np.random.default_rng(6)
    x = rng.normal(0, 1, 400)
    y = rng.normal(0, 10, 400)
    fit = _wls(np.column_stack([np.ones_like(x), x]), y)
    assert abs(fit["t"][1]) < 3


def test_weights_shift_the_fit_toward_the_rows_that_carry_them():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = np.array([0.0, 1.0, 2.0, 30.0])
    design = np.column_stack([np.ones_like(x), x])
    unweighted = _wls(design, y)
    downweighted = _wls(design, y, np.array([1.0, 1.0, 1.0, 0.001]))
    assert downweighted["beta"][1] < unweighted["beta"][1]
    assert downweighted["beta"][1] == pytest.approx(1.0, abs=0.1)


# ---------------------------------------------------------------------------
# Blue-blood bias
# ---------------------------------------------------------------------------


def test_brand_premium_recovers_a_planted_market_bias(wide_history):
    result = brand_premium()
    assert "error" not in result, result.get("error")
    assert result["premium_pts_per_sd"] == pytest.approx(
        synth.PLANTED_BRAND_PREMIUM, abs=0.5
    )
    # Planted at this size over ~2000 games, it must be clearly non-zero.
    assert result["t"] < -2
    assert "overpay" in result["interpretation"].lower() or "premium" in result[
        "interpretation"
    ].lower()


def test_brand_premium_reports_the_season_by_season_drift(wide_history):
    """The NIL series. Without this the thesis cannot be dated."""
    result = brand_premium()
    seasons = result["by_season"]
    # The earliest season is absent, and should be: prestige is built from
    # recruiting classes *before* a season, so the first year of any sample has
    # no measured prestige and its games are dropped rather than treated as
    # league-average.
    assert [row["season"] for row in seasons] == sorted(synth.LONG_SEASONS)[1:]
    # Every season carries the same planted premium, so the series should
    # scatter around it — and that is the honest assertion to make. A single
    # season is ~190 games against a 13-point residual, which buys a standard
    # error around 0.65: individual estimates wander by a point or more, and a
    # test with a flat tolerance would just be a bet on the seed. So: each
    # season within three of its own reported standard errors, and the average
    # of the series, which is well powered, close to the truth.
    for row in seasons:
        assert abs(row["premium_pts_per_sd"] - synth.PLANTED_BRAND_PREMIUM) < 3 * row["se"]
    mean_premium = np.mean([row["premium_pts_per_sd"] for row in seasons])
    assert mean_premium == pytest.approx(synth.PLANTED_BRAND_PREMIUM, abs=0.6)


def test_brand_premium_buckets_show_where_the_effect_lands(wide_history):
    buckets = brand_premium()["by_prestige_bucket"]
    assert buckets
    lookup = {row["bucket"]: row for row in buckets}
    # A market overpaying for brand means the prestigious home side falls short
    # of its number, and the unfancied home side beats it.
    assert lookup["home much more prestigious"]["mean_miss_pts"] < 0
    assert lookup["home much less prestigious"]["mean_miss_pts"] > 0


def test_brand_premium_refuses_a_sample_too_thin_to_speak(featured):
    result = brand_premium()
    assert "error" in result
    assert "need at least" in result["error"]


def test_brand_premium_reports_no_games_rather_than_crashing(conn):
    assert "error" in brand_premium()


def test_a_market_with_no_brand_bias_reads_as_no_brand_bias(conn):
    """The negative control. A fair market must not produce a false positive."""
    from gridiron.features.build import build_team_game

    synth.build_wide(conn, brand_premium=0.0)
    build_team_game(conn, list(synth.LONG_SEASONS), refit_curve=False)
    result = brand_premium()
    # The t-statistic is the assertion that matters. A point estimate drifts a
    # few tenths on any finite sample; what must not happen is the tool calling
    # that drift an effect.
    assert abs(result["t"]) < 3
    assert "not distinguishable from zero" in result["interpretation"].lower()
    assert result["premium_pts_per_sd"] == pytest.approx(0.0, abs=0.7)


# ---------------------------------------------------------------------------
# Hidden yardage
# ---------------------------------------------------------------------------


def test_persistence_separates_a_skill_from_what_happened_to_a_team(long_history):
    result = hidden_yardage_persistence()
    assert "error" not in result, result.get("error")
    lookup = {row["metric"]: row for row in result["metrics"]}
    assert "efficiency_margin" in lookup
    assert "fp_margin_pts" in lookup
    # Salvage skill is planted as a fixed per-team property, so it must show up
    # as strongly repeatable. Field position here is generated at random, so it
    # must not.
    assert lookup["salvage_yards_per_rush"]["split_half_r"] > 0.4
    assert lookup["fp_margin_pts"]["split_half_r"] < 0.4


def test_spearman_brown_correction_is_applied_and_points_the_right_way(long_history):
    for row in hidden_yardage_persistence()["metrics"]:
        if row["split_half_r"] > 0:
            assert row["full_season_r"] > row["split_half_r"]


def test_persistence_needs_enough_team_seasons(featured):
    """Three seasons of eight teams is 24 — below the floor, and it says so."""
    result = hidden_yardage_persistence()
    assert result["metrics"] == []
    assert "Not enough data" in result["interpretation"]


def test_persistence_says_what_to_run_when_nothing_is_built(conn):
    assert "features build" in hidden_yardage_persistence()["error"]


def test_market_test_refuses_a_thin_sample(featured):
    result = hidden_yardage_market_test()
    assert "error" in result


def test_market_test_reports_a_coefficient_on_enough_games(long_history):
    result = hidden_yardage_market_test()
    if "error" in result:
        pytest.skip(f"sample still too thin: {result['error']}")
    assert set(result) == {"coefficient", "se", "t", "games", "interpretation"}
    assert result["games"] >= 200
    assert np.isfinite(result["coefficient"])


# ---------------------------------------------------------------------------
# Portal flow — the mechanism the NIL thesis requires
# ---------------------------------------------------------------------------


def test_portal_flow_runs_out_of_the_prestigious_programs(wide_history):
    frame = portal_and_prestige()
    assert not frame.empty
    # Planted so blue bloods export talent: the slope of net portal rating on
    # prestige has to come out negative.
    assert (frame["slope_rating_per_sd"] < 0).all()
    assert (frame["mean_net_top_quartile"] < frame["mean_net_bottom_quartile"]).all()


def test_portal_analysis_is_empty_rather_than_wrong_without_portal_data(conn):
    """No portal rows must degrade to an empty answer, not an exception."""
    from gridiron.features.build import build_team_game

    synth.build_wide(conn, with_portal=False)
    build_team_game(conn, list(synth.LONG_SEASONS), refit_curve=False)
    assert portal_and_prestige().empty


def test_a_league_too_small_to_speak_gets_no_portal_answer(long_history):
    """Eight teams is below the 30 the per-season regression requires."""
    assert portal_and_prestige().empty


# ---------------------------------------------------------------------------
# The curve behind every hidden-yardage number
# ---------------------------------------------------------------------------


def test_fp_curve_table_is_ordered_and_slopes(long_history):
    curve = fp_curve_table()
    assert not curve.empty
    assert list(curve["yards_to_goal_bin"]) == sorted(curve["yards_to_goal_bin"])
    assert curve["expected_points"].iloc[0] > curve["expected_points"].iloc[-1]
