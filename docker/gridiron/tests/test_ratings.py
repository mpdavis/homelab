"""The rating engine: does it recover the truth, and does it stay in the past."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import synth

from gridiron.models import build_model, model_names
from gridiron.models.ratings import FCS, RidgeRatings
from gridiron.models.weighting import Weighting

FLAT = Weighting(kind="uniform", season_carryover=1.0)


def _fit(response="margin", ridge_lambda=1.0, weighting=FLAT, **kwargs):
    frame = synth.history_frame(**kwargs)
    cutoff = frame["kickoff"].max() + pd.Timedelta(days=1)
    return RidgeRatings(response, weighting, ridge_lambda).fit(frame, cutoff), frame


def test_ridge_recovers_known_ratings():
    """Recovery is approximate by design, and biased toward zero.

    Ratings are only identified up to an additive shift, so the comparison is
    made after centring. And ridge *deliberately* shrinks: a recovered 12.2 for
    a true 14 is the penalty working, not the fit failing. The tolerance is
    wide enough to allow that and narrow enough to catch a sign error or a
    mis-built design matrix.
    """
    fitted, _ = _fit()
    truth = synth.TRUE_RATINGS
    recovered = {team: fitted.rating(team) for team in truth}
    offset = np.mean(list(recovered.values())) - np.mean(list(truth.values()))
    for team, expected in truth.items():
        assert recovered[team] - offset == pytest.approx(expected, abs=2.5)


def test_shrinkage_compresses_the_spread_of_ratings():
    """A statement about the vector, not about any one team.

    Noise can push a single rating past its true value even under a penalty;
    what shrinkage guarantees is that the fitted ratings are collectively less
    spread out than the truth, never more.
    """
    fitted, _ = _fit()
    recovered = np.array([fitted.rating(team) for team in synth.TRUE_RATINGS])
    truth = np.array(list(synth.TRUE_RATINGS.values()))
    assert recovered.std() < truth.std()


def test_ridge_recovers_home_field_advantage():
    fitted, _ = _fit()
    assert fitted.hfa == pytest.approx(synth.TRUE_HFA, abs=1.0)


def test_ranking_order_survives_noise():
    fitted, _ = _fit()
    order = sorted(synth.TEAMS, key=lambda team: -fitted.rating(team))
    assert order == sorted(synth.TEAMS, key=lambda team: -synth.TRUE_RATINGS[team])


def test_stronger_shrinkage_pulls_ratings_toward_zero():
    loose, _ = _fit(ridge_lambda=1.0)
    tight, _ = _fit(ridge_lambda=5000.0)
    spread_loose = np.std(list(loose.ratings.values()))
    spread_tight = np.std(list(tight.ratings.values()))
    assert spread_tight < spread_loose / 4


# ---------------------------------------------------------------------------
# Leakage. This is the property everything else in the package depends on.
# ---------------------------------------------------------------------------


def test_fit_uses_only_games_that_finished_before_the_cutoff():
    frame = synth.history_frame()
    cutoff = pd.Timestamp("2022-09-15", tz="UTC")
    fitted = RidgeRatings("margin", FLAT, 1.0).fit(frame, cutoff)
    assert fitted.n_train == int((frame["kickoff"] < cutoff).sum())
    assert fitted.n_train < len(frame)


def test_a_game_kicking_off_exactly_at_the_cutoff_is_excluded():
    """Strictly before, not on or before: the game being predicted is at the cutoff."""
    frame = synth.history_frame()
    cutoff = frame["kickoff"].iloc[10]
    fitted = RidgeRatings("margin", FLAT, 1.0).fit(frame, cutoff)
    assert fitted.n_train == int((frame["kickoff"] < cutoff).sum())


def test_rewriting_the_future_cannot_change_a_past_fit():
    """The blunt test: corrupt every later result and the fit must not move."""
    frame = synth.history_frame()
    cutoff = pd.Timestamp("2022-09-15", tz="UTC")
    honest = RidgeRatings("margin", FLAT, 1.0).fit(frame, cutoff)

    tampered = frame.copy()
    future = tampered["kickoff"] >= cutoff
    tampered.loc[future, "margin"] = 500.0

    leaky = RidgeRatings("margin", FLAT, 1.0).fit(tampered, cutoff)
    assert leaky.n_train == honest.n_train
    for team in honest.ratings:
        assert leaky.rating(team) == pytest.approx(honest.rating(team), abs=1e-9)
    assert leaky.hfa == pytest.approx(honest.hfa, abs=1e-9)


def test_naive_timezone_cutoff_is_handled_not_crashed():
    frame = synth.history_frame()
    aware = RidgeRatings("margin", FLAT, 1.0).fit(frame, pd.Timestamp("2022-09-15", tz="UTC"))
    naive = RidgeRatings("margin", FLAT, 1.0).fit(frame, pd.Timestamp("2022-09-15"))
    assert naive.n_train == aware.n_train


def test_a_cutoff_before_everything_yields_no_ratings():
    frame = synth.history_frame()
    fitted = RidgeRatings("margin", FLAT, 1.0).fit(frame, pd.Timestamp("2000-01-01", tz="UTC"))
    assert fitted.n_train == 0
    assert fitted.ratings == {}
    assert np.isnan(fitted.predict(frame.head(3))).all()


# ---------------------------------------------------------------------------
# Pooling and prediction
# ---------------------------------------------------------------------------


def test_non_fbs_opponents_are_pooled_into_one_rating():
    frame = synth.history_frame()
    frame.loc[frame["away_team"] == "Hotel", "away_team"] = "Directional State"
    engine = RidgeRatings("margin", FLAT, 1.0)
    engine.fbs = set(synth.TEAMS)
    engine.fit(frame, frame["kickoff"].max() + pd.Timedelta(days=1))
    assert "Directional State" not in engine.ratings
    assert FCS in engine.ratings
    # An unseen team is an FCS opponent, not a league-average one.
    assert engine.rating("Nobody At All") == engine.ratings[FCS]


def test_neutral_sites_drop_the_home_field_term():
    fitted, frame = _fit()
    fixtures = frame.head(1).copy()
    fixtures["neutral_site"] = False
    at_home = fitted.predict(fixtures)[0]
    fixtures["neutral_site"] = True
    at_neutral = fitted.predict(fixtures)[0]
    assert at_home - at_neutral == pytest.approx(fitted.hfa)


# ---------------------------------------------------------------------------
# The registered models
# ---------------------------------------------------------------------------


def test_every_registered_model_accepts_sweep_parameters():
    """`run_sweep` injects weighting kwargs into whatever model it is given."""
    injected = dict(
        weighting_kind="exponential",
        half_life_days=200.0,
        window_days=None,
        season_carryover=0.7,
    )
    for name in model_names():
        model = build_model(name, **injected)
        assert hasattr(model, "fit") and hasattr(model, "predict")


def test_decomposed_sums_its_two_halves_back_to_a_margin():
    frame = synth.history_frame()
    cutoff = frame["kickoff"].max() + pd.Timedelta(days=1)
    model = build_model("decomposed", ridge_lambda=1.0, fp_ridge_lambda=1.0)
    model.fbs = set(synth.TEAMS)
    model.fit(frame, cutoff)
    fixtures = frame.tail(4)
    total = model.predict(fixtures)
    parts = model.efficiency.predict(fixtures) + model.field_position.predict(fixtures)
    assert total == pytest.approx(parts)


def test_field_position_component_carries_no_home_field_term():
    """Both halves owning an HFA column would count it twice."""
    model = build_model("decomposed")
    assert model.field_position.fit_hfa is False
    assert model.efficiency.fit_hfa is True


def test_market_model_is_the_market():
    frame = synth.history_frame()
    model = build_model("market")
    model.fit(frame, frame["kickoff"].max())
    assert model.predict(frame.tail(5)) == pytest.approx(
        frame.tail(5)["market_margin"].to_numpy()
    )


def test_market_debias_rejects_a_frame_without_the_columns_it_needs():
    model = build_model("market_debias")
    with pytest.raises(ValueError, match="market_debias needs columns"):
        model.fit(pd.DataFrame({"kickoff": []}), pd.Timestamp("2023-01-01", tz="UTC"))


def test_market_debias_stays_neutral_on_a_thin_sample():
    """Below min_games it must return the market untouched, not a wild fit."""
    frame = synth.history_frame().head(20)
    model = build_model("market_debias", min_games=400)
    model.fit(frame, frame["kickoff"].max() + pd.Timedelta(days=1))
    assert model.premium == 0.0
    assert model.predict(frame) == pytest.approx(frame["market_margin"].to_numpy())


def test_market_debias_recovers_a_planted_brand_premium():
    frame = synth.history_frame(noise=3.0)
    rng = np.random.default_rng(11)
    gap = rng.normal(0.0, 1.0, len(frame))
    frame["prestige_gap"] = gap
    # Plant the thesis: the market overpays the prestigious side by 2 points
    # per standard deviation, so results fall short of the line by that much.
    frame["market_margin"] = frame["margin"] + 2.0 * gap
    model = build_model("market_debias", min_games=50, half_life_days=100000.0)
    model.fit(frame, frame["kickoff"].max() + pd.Timedelta(days=1))
    assert model.premium == pytest.approx(-2.0, abs=0.25)


def test_market_debias_clamps_its_adjustment():
    frame = synth.history_frame().head(3).copy()
    model = build_model("market_debias", max_adjustment=3.0)
    model.premium, model.intercept = -100.0, 0.0
    frame["prestige_gap"] = [5.0, -5.0, 0.0]
    adjusted = model.predict(frame) - frame["market_margin"].to_numpy()
    assert adjusted == pytest.approx([-3.0, 3.0, 0.0])


def test_elo_produces_finite_predictions_and_orders_teams_sensibly():
    frame = synth.history_frame()
    cutoff = frame["kickoff"].max() + pd.Timedelta(days=1)
    model = build_model("elo")
    model.fit(frame, cutoff)
    fixtures = pd.DataFrame(
        [
            {"home_team": "Alpha", "away_team": "Hotel", "neutral_site": True},
            {"home_team": "Hotel", "away_team": "Alpha", "neutral_site": True},
        ]
    )
    predictions = model.predict(fixtures)
    assert np.isfinite(predictions).all()
    assert predictions[0] > 0 > predictions[1]
