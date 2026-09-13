"""The feature layer: the field-position curve and the hidden-yardage columns."""

from __future__ import annotations

import numpy as np
import pytest

import synth

from gridiron.features import block_columns, registered_blocks
from gridiron.features.build import build_team_game, fit_fp_curve
from gridiron.features.prestige import prestige_gap, team_season_prestige


# ---------------------------------------------------------------------------
# The expected-points curve
# ---------------------------------------------------------------------------


def test_curve_is_worth_more_points_closer_to_the_goal_line(populated):
    curve = fit_fp_curve(populated).sort_values("yards_to_goal_bin")
    near = curve[curve["yards_to_goal_bin"] <= 30]["expected_points"].mean()
    far = curve[curve["yards_to_goal_bin"] >= 70]["expected_points"].mean()
    assert near > far
    # Correlation, not just the endpoints: the whole curve has to slope.
    assert np.corrcoef(curve["yards_to_goal_bin"], curve["expected_points"])[0, 1] < -0.8


def test_curve_is_persisted_under_its_fit_key(populated):
    fit_fp_curve(populated, fit_key="vintage-2022")
    rows = populated.execute(
        "SELECT count(*) FROM fp_curve WHERE fit_key = 'vintage-2022'"
    ).fetchone()[0]
    assert rows > 0


def test_a_point_in_time_curve_sees_less_data(populated):
    everything = fit_fp_curve(populated, fit_key="global")
    early = fit_fp_curve(
        populated, fit_key="early", as_of=synth._kickoff(2022, 1, 0)
    )
    assert early["drives"].sum() < everything["drives"].sum()


def test_fitting_a_curve_with_no_drives_is_an_error_not_an_empty_curve(conn):
    with pytest.raises(ValueError, match="No drives"):
        fit_fp_curve(conn)


def test_end_of_half_kneeldowns_are_excluded_from_the_curve(populated):
    """They start wherever the last play died and are worth nothing by design."""
    before = fit_fp_curve(populated, fit_key="before")["drives"].sum()
    populated.execute(
        """
        UPDATE drives SET drive_result = 'END OF HALF'
        WHERE drive_number = 11
        """
    )
    after = fit_fp_curve(populated, fit_key="after")["drives"].sum()
    assert after < before


# ---------------------------------------------------------------------------
# team_game
# ---------------------------------------------------------------------------


def test_build_writes_two_rows_per_game(featured):
    games, rows = featured.execute(
        "SELECT (SELECT count(*) FROM games), (SELECT count(*) FROM team_game)"
    ).fetchone()
    assert rows == games * 2


def test_margins_are_equal_and_opposite(featured):
    frame = featured.execute(
        """
        SELECT a.margin AS home_margin, b.margin AS away_margin
        FROM team_game a
        JOIN team_game b ON a.game_id = b.game_id AND a.team = b.opponent
        WHERE a.is_home
        """
    ).df()
    assert (frame["home_margin"] == -frame["away_margin"]).all()


def test_the_decomposition_adds_back_up_to_the_margin(featured):
    """efficiency_margin + fp_margin_pts must be the scoreboard, exactly."""
    frame = featured.execute(
        "SELECT margin, efficiency_margin, fp_margin_pts FROM team_game"
    ).df()
    reconstructed = frame["efficiency_margin"] + frame["fp_margin_pts"].fillna(0.0)
    assert reconstructed.to_numpy() == pytest.approx(frame["margin"].to_numpy())


def test_field_position_margins_are_equal_and_opposite(featured):
    frame = featured.execute(
        """
        SELECT a.fp_margin_pts AS home_fp, b.fp_margin_pts AS away_fp
        FROM team_game a
        JOIN team_game b ON a.game_id = b.game_id AND a.team = b.opponent
        WHERE a.is_home
        """
    ).df()
    assert frame["home_fp"].to_numpy() == pytest.approx(-frame["away_fp"].to_numpy())


def test_turnover_luck_is_reported_but_not_subtracted(featured):
    """Subtracting it as well as field position would double-count a turnover."""
    frame = featured.execute(
        "SELECT margin, efficiency_margin, fp_margin_pts, turnover_luck_pts "
        "FROM team_game WHERE turnover_luck_pts <> 0 LIMIT 50"
    ).df()
    assert not frame.empty, "the synthetic universe should contain turnovers"
    assert (
        frame["efficiency_margin"] != frame["margin"] - frame["turnover_luck_pts"]
    ).any()


def test_salvage_recovers_the_planted_skill_ordering(featured):
    """The 8-yard-loss-into-1-yard-loss metric, checked against known truth."""
    frame = featured.execute(
        "SELECT team, avg(salvage_yards_per_rush) AS salvage "
        "FROM team_game GROUP BY team"
    ).df()
    salvage = dict(zip(frame["team"], frame["salvage"]))
    truth = synth.SALVAGE_SKILL
    order = sorted(salvage, key=lambda team: -salvage[team])
    best, worst = order[0], order[-1]
    assert truth[best] > truth[worst]
    correlation = np.corrcoef(
        [salvage[team] for team in synth.TEAMS],
        [truth[team] for team in synth.TEAMS],
    )[0, 1]
    assert correlation > 0.8


def test_salvage_is_zero_rather_than_null_when_nothing_was_stuffed(featured):
    """A null would silently drop the game from any model reading the column."""
    nulls = featured.execute(
        "SELECT count(*) FROM team_game WHERE salvage_yards_per_rush IS NULL"
    ).fetchone()[0]
    assert nulls == 0


def test_every_registered_block_declares_the_columns_it_writes(featured):
    columns = set(
        row[0]
        for row in featured.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'team_game'"
        ).fetchall()
    )
    assert set(block_columns()) <= columns
    assert {"field_position", "negative_plays", "efficiency"} <= set(registered_blocks())


def test_rebuilding_is_idempotent(featured):
    before = featured.execute("SELECT count(*) FROM team_game").fetchone()[0]
    build_team_game(featured, list(synth.SEASONS))
    after = featured.execute("SELECT count(*) FROM team_game").fetchone()[0]
    assert before == after


def test_building_a_season_with_no_games_leaves_the_table_alone(featured):
    before = featured.execute("SELECT count(*) FROM team_game").fetchone()[0]
    assert build_team_game(featured, [1999]) == 0
    assert featured.execute("SELECT count(*) FROM team_game").fetchone()[0] == before


# ---------------------------------------------------------------------------
# Prestige
# ---------------------------------------------------------------------------


def test_prestige_is_standardised_within_a_season(populated):
    prestige = team_season_prestige(populated)
    assert not prestige.empty
    for _, group in prestige.groupby("season"):
        if len(group) < 2:
            continue
        assert group["prestige"].mean() == pytest.approx(0.0, abs=1e-9)


def test_prestige_ranks_the_bluest_blood_first(populated):
    prestige = team_season_prestige(populated)
    latest = prestige[prestige["season"] == prestige["season"].max()]
    top = latest.sort_values("prestige", ascending=False)["team"].iloc[0]
    assert top == "Alpha"


def test_prestige_gap_is_home_minus_away(populated):
    import pandas as pd

    prestige = team_season_prestige(populated)
    games = pd.DataFrame(
        [
            {"season": 2023, "home_team": "Alpha", "away_team": "Hotel"},
            {"season": 2023, "home_team": "Hotel", "away_team": "Alpha"},
        ]
    )
    gapped = prestige_gap(prestige, games)
    assert gapped["prestige_gap"].iloc[0] > 0
    assert gapped["prestige_gap"].iloc[0] == pytest.approx(
        -gapped["prestige_gap"].iloc[1]
    )
