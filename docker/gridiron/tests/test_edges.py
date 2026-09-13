"""The betting arithmetic. Every sign error here costs real money."""

from __future__ import annotations

import math

import pytest

from gridiron.backtest import american_to_profit, breakeven_rate
from gridiron.edges import (
    DEFAULT_KELLY_FRACTION,
    best_price_per_game,
    cover_probability,
    devig,
    expected_value,
    implied_probability,
    kelly_stake,
)


def test_american_odds_convert_both_directions():
    assert american_to_profit(-110) == pytest.approx(100 / 110)
    assert american_to_profit(100) == pytest.approx(1.0)
    assert american_to_profit(150) == pytest.approx(1.5)


def test_the_standard_price_breaks_even_at_52_38_percent():
    assert breakeven_rate(-110) == pytest.approx(0.523809, abs=1e-5)
    assert breakeven_rate(100) == pytest.approx(0.5)


def test_implied_probability_includes_the_vig():
    assert implied_probability(-110) == pytest.approx(110 / 210)
    assert implied_probability(100) == pytest.approx(0.5)
    assert implied_probability(150) == pytest.approx(0.4)
    assert math.isnan(implied_probability(float("nan")))


def test_devig_removes_the_hold_and_leaves_a_probability_pair():
    home, away = devig(-110, -110)
    assert home + away == pytest.approx(1.0)
    assert home == pytest.approx(0.5)

    # A two-sided market with the hold on one side still has to sum to one,
    # and the favourite has to stay the favourite.
    home, away = devig(-200, 170)
    assert home + away == pytest.approx(1.0)
    assert home > away


def test_devig_is_defensive_about_nonsense():
    home, away = devig(float("nan"), -110)
    assert math.isnan(home) and math.isnan(away)


def test_cover_probability_is_a_normal_cdf_of_the_edge():
    assert cover_probability(0.0) == pytest.approx(0.5)
    assert cover_probability(3.0) > 0.5 > cover_probability(-3.0)
    assert cover_probability(3.0) + cover_probability(-3.0) == pytest.approx(1.0)
    # One sigma of edge is the usual 84%.
    assert cover_probability(13.5, sigma=13.5) == pytest.approx(0.8413, abs=1e-3)
    # A tighter model turns the same edge into more confidence.
    assert cover_probability(3.0, sigma=10.0) > cover_probability(3.0, sigma=16.0)


def test_cover_probability_rejects_an_impossible_sigma():
    with pytest.raises(ValueError):
        cover_probability(3.0, sigma=0.0)


def test_expected_value_is_zero_at_the_breakeven_rate():
    assert expected_value(breakeven_rate(-110), -110) == pytest.approx(0.0, abs=1e-12)
    assert expected_value(0.60, -110) > 0
    assert expected_value(0.50, -110) < 0


def test_kelly_refuses_to_stake_without_an_advantage():
    assert kelly_stake(0.50, -110) == 0.0
    assert kelly_stake(breakeven_rate(-110), -110) == pytest.approx(0.0, abs=1e-12)


def test_kelly_is_fractional_and_grows_with_the_edge():
    small = kelly_stake(0.55, -110)
    large = kelly_stake(0.62, -110)
    assert 0 < small < large < 1
    # Quarter-Kelly by default: exactly a quarter of the full-Kelly number.
    full = kelly_stake(0.55, -110, fraction=1.0)
    assert small == pytest.approx(full * DEFAULT_KELLY_FRACTION)
    assert DEFAULT_KELLY_FRACTION == 0.25


def test_line_shopping_keeps_the_best_number_and_prices_the_difference():
    import pandas as pd

    edges = pd.DataFrame(
        [
            {
                "game_id": 1,
                "side": "home",
                "book": "draftkings",
                "bet_team": "Alpha",
                "bet_spread": -3.0,
                "edge": 4.0,
                "ev_per_unit": 0.05,
            },
            {
                "game_id": 1,
                "side": "home",
                "book": "fanatics",
                "bet_team": "Alpha",
                "bet_spread": -1.5,
                "edge": 5.5,
                "ev_per_unit": 0.09,
            },
            {
                "game_id": 2,
                "side": "pass",
                "book": "draftkings",
                "bet_team": None,
                "bet_spread": -7.0,
                "edge": 0.2,
                "ev_per_unit": -0.04,
            },
        ]
    )
    best = best_price_per_game(edges)
    assert len(best) == 1
    row = best.iloc[0]
    assert row["book"] == "fanatics"
    assert row["shop_gain_pts"] == pytest.approx(1.5)
