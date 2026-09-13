"""Building a market series from whatever books a season actually has.

This exists because of a bug that reached production. `DEFAULT_PROVIDER` was
the literal string "consensus", which CFBD published through 2022 and then
stopped. From 2023 the join matched nothing, and because a missing line drops
a game rather than raising, every analysis silently lost the NIL era while
still reporting confident numbers over the seasons that remained.

So the tests here are less about medians than about that failure mode: a
provider mix that changes mid-decade must not quietly shrink the sample.
"""

from __future__ import annotations

import pytest

import synth

from gridiron.backtest import DEFAULT_PROVIDER, load_frame, line_series


def test_every_season_has_a_market_line(featured):
    """The regression. Seasons after the consensus row disappeared must survive."""
    frame = load_frame(featured)
    priced = frame.dropna(subset=["market_margin"])
    assert set(priced["season"]) == set(synth.SEASONS)

    modern = priced[priced["season"] > synth.CONSENSUS_LAST_SEASON]
    legacy = priced[priced["season"] <= synth.CONSENSUS_LAST_SEASON]
    assert not modern.empty, "seasons priced only by individual books were dropped"
    assert not legacy.empty


def test_no_game_loses_its_line_to_the_provider_change(featured):
    """Coverage must not step down when the provider mix changes."""
    frame = load_frame(featured)
    by_season = frame.groupby("season")["market_margin"].apply(
        lambda s: s.notna().mean()
    )
    assert by_season.min() > 0.95, dict(by_season)


def test_the_consensus_is_the_median_across_books(featured):
    """One game, checked by hand against the rows behind it."""
    frame = load_frame(featured)
    game_id = int(frame[frame["season"] > synth.CONSENSUS_LAST_SEASON]["game_id"].iloc[0])

    spreads = [
        row[0]
        for row in featured.execute(
            "SELECT spread FROM lines WHERE game_id = ? ORDER BY spread", [game_id]
        ).fetchall()
    ]
    assert len(spreads) > 1, "expected several books for this game"
    expected = sorted(spreads)[len(spreads) // 2] if len(spreads) % 2 else (
        sum(sorted(spreads)[len(spreads) // 2 - 1 : len(spreads) // 2 + 1]) / 2
    )
    got = float(frame[frame["game_id"] == game_id]["spread"].iloc[0])
    assert got == pytest.approx(expected)


def test_cfbds_own_aggregate_does_not_double_count_itself(featured):
    """Where real books exist the merged row is excluded — it is their average,
    and counting it alongside its own inputs would weight it twice."""
    season = min(synth.SEASONS)
    game_id = int(
        featured.execute(
            "SELECT game_id FROM games WHERE season = ? LIMIT 1", [season]
        ).fetchone()[0]
    )
    # Move the aggregate far away from the books. If it were included, the
    # median would shift; excluded, it cannot.
    featured.execute(
        "UPDATE lines SET spread = 400 WHERE game_id = ? AND provider = 'consensus'",
        [game_id],
    )
    frame = load_frame(featured)
    got = float(frame[frame["game_id"] == game_id]["spread"].iloc[0])
    assert abs(got) < 100


def test_the_aggregate_is_used_when_it_is_all_there_is(featured):
    """A game quoted only by CFBD's merged row still gets a line."""
    game_id = int(featured.execute("SELECT game_id FROM lines LIMIT 1").fetchone()[0])
    featured.execute(
        "DELETE FROM lines WHERE game_id = ? AND lower(provider) <> 'consensus'",
        [game_id],
    )
    featured.execute(
        "INSERT INTO lines (game_id, provider, spread, spread_open) "
        "SELECT ?, 'consensus', -7.0, -7.0 "
        "WHERE NOT EXISTS (SELECT 1 FROM lines WHERE game_id = ? AND provider = 'consensus')",
        [game_id, game_id],
    )
    frame = load_frame(featured)
    row = frame[frame["game_id"] == game_id]
    assert row["market_margin"].notna().all()


def test_an_explicit_provider_selects_that_book_alone(featured):
    frame = load_frame(featured, provider="ESPN Bet")
    priced = frame.dropna(subset=["market_margin"])
    # ESPN Bet only quotes the later seasons in this universe.
    assert set(priced["season"]) <= {
        s for s in synth.SEASONS if s > synth.CONSENSUS_LAST_SEASON
    }
    assert not priced.empty


def test_provider_matching_is_case_insensitive(featured):
    assert len(load_frame(featured, provider="espn bet").dropna(subset=["market_margin"])) == len(
        load_frame(featured, provider="ESPN Bet").dropna(subset=["market_margin"])
    )


def test_an_unknown_provider_yields_no_lines_rather_than_wrong_ones(featured):
    frame = load_frame(featured, provider="Nonexistent Book")
    assert frame["market_margin"].isna().all()


def test_line_series_parameterises_only_for_an_explicit_provider():
    sql, params = line_series(DEFAULT_PROVIDER)
    assert params == []
    assert "median" in sql.lower()

    sql, params = line_series("Bovada")
    assert params == ["Bovada"]


def test_provider_aliases_collapse_the_same_book(featured):
    """CFBD has spelled DraftKings two ways; unaliased that is two half-seasons."""
    from gridiron.sources.cfbd import PROVIDER_ALIASES

    assert PROVIDER_ALIASES["draft kings"] == "DraftKings"
