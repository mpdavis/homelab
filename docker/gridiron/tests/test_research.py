"""The guardrails around an automated search.

Most of these test refusals rather than results. A search harness that works is
easy; one that cannot quietly spend its holdout, cannot lose count of how many
times it looked, and cannot be talked into running `DROP TABLE` is the thing
worth having.
"""

from __future__ import annotations

import math

import pytest

import synth

from gridiron.research import evaluate as ev
from gridiron.research import registry, sqlguard
from gridiron.research.registry import Hypothesis


# ---------------------------------------------------------------------------
# The holdout
# ---------------------------------------------------------------------------


def test_search_and_holdout_partition_the_seasons(featured, monkeypatch):
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    search = registry.search_seasons(featured)
    held = registry.holdout_seasons(featured)
    assert set(search) & set(held) == set()
    assert set(search) | set(held) == set(synth.SEASONS)
    assert max(search) < min(held)


def test_a_search_stage_cannot_touch_the_holdout(featured, monkeypatch):
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    with pytest.raises(registry.HoldoutViolation, match="reserved"):
        registry.guard_seasons(featured, [2022, 2023], stage="backtest")


def test_the_guard_refuses_rather_than_silently_filtering(featured, monkeypatch):
    """Dropping the seasons quietly would let a caller believe it tested a
    decade when it tested eight years."""
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    with pytest.raises(registry.HoldoutViolation):
        registry.guard_seasons(featured, list(synth.SEASONS), stage="persistence")
    # The holdout stage is the one place they are allowed.
    assert registry.guard_seasons(featured, [2023], stage="holdout") == [2023]


# ---------------------------------------------------------------------------
# Multiple testing
# ---------------------------------------------------------------------------


def test_the_bar_rises_with_the_number_of_looks():
    bars = [registry.required_t(n) for n in (1, 10, 100, 1000)]
    assert bars == sorted(bars)
    assert bars[0] == pytest.approx(1.96, abs=0.01)
    assert bars[-1] > 4.0


def test_expected_max_t_matches_the_known_asymptotic():
    assert registry.expected_max_t(200) == pytest.approx(math.sqrt(2 * math.log(200)))
    assert registry.expected_max_t(200) == pytest.approx(3.26, abs=0.05)
    assert registry.expected_max_t(1) == 0.0


def test_a_result_that_would_pass_alone_fails_after_many_looks():
    """The whole point. t=2.5 is significant once and meaningless after 500 tries."""
    alone = registry.assess(2.5, n_trials=1)
    searched = registry.assess(2.5, n_trials=500)
    assert alone["clears_corrected_bar"] is True
    assert searched["clears_corrected_bar"] is False
    assert "short of" in searched["verdict"]


def test_trials_are_counted_and_the_count_drives_the_bar(conn):
    hypothesis = Hypothesis(name="x", mechanism="m", sql="SELECT 1")
    registry.record_hypothesis(hypothesis)
    before = registry.required_t(registry.trial_count())
    for _ in range(20):
        registry.record_trial(
            hypothesis.hypothesis_id, "backtest", seasons=[2021],
            passed=False, statistic=0.4,
        )
    assert registry.trial_count() == 20
    assert registry.required_t(registry.trial_count()) > before


def test_cheap_filter_trials_do_not_inflate_the_bar(conn):
    """Selection happens at the backtest, so that is what is counted."""
    hypothesis = Hypothesis(name="x", mechanism="m", sql="SELECT 1")
    registry.record_hypothesis(hypothesis)
    for stage in ("persistence", "market", "persistence"):
        registry.record_trial(
            hypothesis.hypothesis_id, stage, seasons=[2021], passed=True, statistic=1.0
        )
    assert registry.trial_count("backtest") == 0
    assert registry.trial_count(None) == 3


# ---------------------------------------------------------------------------
# The SQL guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE games",
        "DELETE FROM games",
        "UPDATE games SET home_points = 0",
        "INSERT INTO games VALUES (1)",
        "CREATE TABLE evil AS SELECT 1",
        "ATTACH 'other.db' AS other",
        "COPY (SELECT * FROM games) TO '/tmp/out.csv'",
    ],
)
def test_anything_that_is_not_a_select_is_refused(sql):
    with pytest.raises(sqlguard.UnsafeSQL):
        sqlguard.validate(sql)


def test_a_select_smuggling_a_second_statement_is_refused():
    with pytest.raises(sqlguard.UnsafeSQL, match="exactly one statement"):
        sqlguard.validate("SELECT 1; DROP TABLE games")


def test_file_reading_functions_are_refused():
    with pytest.raises(sqlguard.UnsafeSQL, match="outside the database"):
        sqlguard.validate("SELECT * FROM read_csv('/etc/passwd')")


def test_the_word_delete_in_a_comment_is_not_a_rejection():
    """A parse, not a keyword scan — this must pass."""
    sql = "SELECT 1 AS game_id -- we do not delete anything here\n"
    assert sqlguard.validate(sql)


def test_a_plain_select_passes():
    assert sqlguard.validate("SELECT game_id, team, 1.0 AS m FROM team_game")


def test_a_block_must_return_game_id_and_team(featured):
    with pytest.raises(sqlguard.UnsafeSQL, match="missing"):
        sqlguard.run(featured, "SELECT season FROM team_game")


def test_a_block_must_return_a_numeric_metric(featured):
    with pytest.raises(sqlguard.UnsafeSQL, match="no numeric metric"):
        sqlguard.run(featured, "SELECT game_id, team FROM team_game")


def test_a_well_formed_block_runs_and_reports_its_metrics(featured):
    frame, metrics = sqlguard.run(
        featured,
        "SELECT game_id, team, success_rate AS m FROM team_game "
        "WHERE success_rate IS NOT NULL",
    )
    assert metrics == ["m"]
    assert not frame.empty


# ---------------------------------------------------------------------------
# The funnel
# ---------------------------------------------------------------------------


def _hypothesis(sql: str, name: str = "test") -> Hypothesis:
    h = Hypothesis(name=name, mechanism="a stated reason", sql=sql)
    registry.record_hypothesis(h)
    return h


def test_a_metric_that_does_not_repeat_is_rejected_before_the_backtest(
    long_history, monkeypatch
):
    """Noise must die at the first gate and never reach the sample.

    The metric is a hash of game_id — deterministic, and a property of the
    fixture rather than of the team, so the two halves of a season hold
    unrelated numbers. `random()` would do the same job but not reproducibly:
    at 48 team-seasons the null spread of r is about 0.15, so a bare threshold
    would let a third of noise draws through and the test would flake.
    """
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    h = _hypothesis(
        "SELECT game_id, team, (hash(game_id) % 97)::DOUBLE AS m FROM team_game",
        name="pure_noise",
    )
    report = ev.evaluate(h)
    assert report["stages"]["persistence"]["passed"] is False
    assert "backtest" not in report["stages"]
    assert registry.trial_count("backtest") == 0
    assert "does not repeat" in report["outcome"]


def test_a_planted_team_property_clears_the_repeatability_gate(
    long_history, monkeypatch
):
    """salvage_yards_per_rush is generated as a fixed per-team skill, so it
    must read as repeatable — the positive control for the gate above."""
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    h = _hypothesis(
        "SELECT game_id, team, salvage_yards_per_rush AS m FROM team_game",
        name="planted_salvage",
    )
    report = ev.evaluate(h, stage_limit="market")
    persistence = report["stages"]["persistence"]
    assert persistence["passed"] is True
    assert persistence["split_half_r"] > 0.5
    assert persistence["z"] > 2.0


def test_a_gate_needs_significance_not_just_a_big_looking_r():
    """An r that a small sample could easily produce by chance is not evidence."""
    from gridiron.research.evaluate import MIN_SPLIT_HALF_R, MIN_SPLIT_HALF_Z

    assert MIN_SPLIT_HALF_Z >= 2.0
    # r = 0.2 over 20 team-seasons: z = atanh(0.2)*sqrt(17) = 0.84, nowhere near.
    z = math.atanh(0.2) * math.sqrt(20 - 3)
    assert 0.2 >= MIN_SPLIT_HALF_R and z < MIN_SPLIT_HALF_Z


def test_too_little_data_is_reported_as_unjudged_not_as_a_rejection(
    featured, monkeypatch
):
    """Three seasons of eight teams is 24 team-seasons — below the floor.

    "Cannot tell" and "measured, and it does not repeat" are different
    findings, and the ledger keeps the wording forever. Recording a rejection
    here would retire an idea that was never actually tested.
    """
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    h = _hypothesis(
        "SELECT game_id, team, salvage_yards_per_rush AS m FROM team_game",
        name="thin_sample",
    )
    report = ev.evaluate(h)
    assert "not judged" in report["outcome"]
    assert "not a verdict" in report["outcome"]

    with __import__("gridiron.db", fromlist=["cursor"]).cursor() as conn2:
        status = conn2.execute(
            "SELECT status FROM research_hypotheses WHERE hypothesis_id = ?",
            [h.hypothesis_id],
        ).fetchone()[0]
    assert status == "proposed", "an unjudged idea must not be marked rejected"


def test_a_thin_market_sample_is_also_unjudged_rather_than_priced(
    long_history, monkeypatch
):
    """The same distinction one gate later: too few priced games is not a
    finding that the market prices the metric."""
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    h = _hypothesis(
        "SELECT game_id, team, salvage_yards_per_rush AS m FROM team_game",
        name="planted_but_thin",
    )
    report = ev.evaluate(h)
    assert report["stages"]["persistence"]["passed"] is True
    market = report["stages"]["market"]
    if not market["passed"] and market.get("games", 0) < ev.MIN_GAMES:
        assert "not judged" in report["outcome"]
        assert "priced" not in report["outcome"]


def test_the_holdout_refuses_a_candidate_that_never_survived(conn):
    h = _hypothesis("SELECT game_id, team, 1.0 AS m FROM team_game", name="unproven")
    with pytest.raises(ValueError, match="not 'survived'"):
        ev.holdout(h.hypothesis_id)


def test_the_holdout_cannot_be_spent_twice(conn):
    h = _hypothesis("SELECT game_id, team, 1.0 AS m FROM team_game", name="finalist")
    registry.set_status(h.hypothesis_id, "spent")
    with pytest.raises(ValueError, match="already had its look"):
        ev.holdout(h.hypothesis_id)


def test_status_summary_reports_the_partition_and_the_bar(featured, monkeypatch):
    from gridiron.config import settings

    monkeypatch.setenv("GRIDIRON_HOLDOUT_FROM_SEASON", "2023")
    settings.cache_clear()
    state = registry.summary()
    assert state["holdout_seasons"] == [2023]
    assert state["required_t_now"] >= 1.9
    assert "expected_max_t_under_null" in state


# ---------------------------------------------------------------------------
# The proposer
# ---------------------------------------------------------------------------


def test_the_sdk_surface_the_proposer_depends_on_exists():
    """Pin the assumption rather than discover it at the first proposal.

    `messages.parse(output_format=...)` is not on anthropic 0.x, and a resolver
    given a loose floor would install a version that imports fine and fails
    only when someone actually asks for a hypothesis.
    """
    import inspect

    import anthropic

    client = anthropic.Anthropic(api_key="not-a-real-key")
    assert hasattr(client.messages, "parse")
    assert "output_format" in inspect.signature(client.messages.parse).parameters


def test_the_proposer_refuses_without_a_key_and_says_what_still_works(conn, monkeypatch):
    from gridiron.config import settings
    from gridiron.research import propose

    monkeypatch.setenv("GRIDIRON_ANTHROPIC_API_KEY", "")
    settings.cache_clear()
    with pytest.raises(RuntimeError, match="research add"):
        propose.propose()
