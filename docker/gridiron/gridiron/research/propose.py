"""Ask Claude for a hypothesis, with its reasoning recorded before the result.

This is the one step where a model earns its place. A grid over ten thousand
SQL aggregations will find correlations reliably, and almost all of them will
be spurious — the search space is far larger than the evidence. What a language
model can do that a grid cannot is propose something *because of a reason*:
"teams off a bye against an opponent on a short week should hold up better late"
is a hypothesis with a mechanism, and a mechanism is what makes an effect more
likely to survive a sample it has not seen.

So the prompt demands the mechanism first and treats the SQL as its expression,
not the other way round. The mechanism is stored before evaluation, which is
what makes it possible afterwards to tell a real prediction from a story
written to fit a number.
"""

from __future__ import annotations

import logging

from ..config import settings
from ..db import cursor
from .registry import Hypothesis

log = logging.getLogger(__name__)

SYSTEM = """You are proposing testable hypotheses for a college football betting
research system. You will be judged on whether your ideas survive out-of-sample
testing, not on whether they sound clever.

Rules that matter:

1. State the MECHANISM first — the causal story for why this edge should exist
   and why the market might not price it. If you cannot name a mechanism, the
   idea is not worth testing. "These numbers might correlate" is not a mechanism.
2. Your SQL must be ONE read-only SELECT returning `game_id`, `team`, and one or
   more numeric metric columns — one row per team per game. No DDL, no CTAS, no
   file functions.
3. Only use data available BEFORE kickoff, or facts about the game itself that
   a per-game metric is allowed to describe. The harness converts your metric
   into a prior average, so you may compute a within-game quantity.
4. Prefer ideas the market plausibly underweights: situational spots, fatigue,
   travel, personnel churn, special teams, drive-level structure. Avoid restating
   scoring margin, which is already the strongest cheap predictor there is.
5. Do not repeat a hypothesis that has already been tried.
"""

SCHEMA_NOTE = """Tables you may read:

games(game_id, season, week, season_type, start_date, neutral_site,
      conference_game, completed, home_team, away_team, home_conference,
      away_conference, home_classification, away_classification,
      home_points, away_points, venue)

drives(drive_id, game_id, season, offense, defense, drive_number, start_period,
       start_yards_to_goal, end_yards_to_goal, plays, yards, drive_result,
       scoring, start_offense_score, start_defense_score, end_offense_score,
       end_defense_score)

plays(play_id, game_id, drive_id, season, offense, defense, period, down,
      distance, yards_to_goal, yards_gained, play_type, scoring, ppa)

team_game(game_id, season, week, kickoff, team, opponent, is_home, neutral_site,
          points, points_allowed, drives, avg_start_yards_to_goal,
          def_start_yards_to_goal, fp_points, def_fp_points, fp_margin_pts,
          rushes, stuff_rate, avg_stuff_yards, salvage_yards_per_rush,
          sacks_taken, avg_sack_yards, plays, success_rate, explosiveness,
          yards_per_play, def_success_rate, def_yards_per_play, margin,
          turnover_margin, turnover_luck_pts, efficiency_margin)

portal(season, first_name, last_name, position, origin, destination,
       transfer_date, rating, stars, eligibility)

recruiting(season, school, rank, points)   talent(season, school, talent)
lines(game_id, provider, spread, spread_open, over_under, home_moneyline)

`play_type` is free text — match with ILIKE patterns, not equality on a guess.
DuckDB SQL. Window functions are available and usually what you want."""


def _already_tried() -> list[str]:
    with cursor() as conn:
        rows = conn.execute(
            "SELECT name, mechanism FROM research_hypotheses ORDER BY created_at DESC "
            "LIMIT 40"
        ).fetchall()
    return [f"- {name}: {mechanism[:160]}" for name, mechanism in rows]


def propose(*, hint: str = "", model: str | None = None) -> Hypothesis:
    """Ask for one hypothesis. Raises if no API key is configured."""
    cfg = settings()
    if not cfg.anthropic_api_key:
        raise RuntimeError(
            "GRIDIRON_ANTHROPIC_API_KEY is not set, so the proposer cannot run. "
            "The rest of the harness works by hand: write the SQL yourself and "
            "use `gridiron research add`."
        )

    import anthropic
    from pydantic import BaseModel, Field

    class Proposal(BaseModel):
        name: str = Field(description="short slug, lowercase with underscores")
        mechanism: str = Field(
            description="why this edge should exist AND why the market may not "
            "price it; two to four sentences"
        )
        expected_sign: str = Field(
            description="'positive' if a higher metric should mean the team "
            "outperforms its line, otherwise 'negative'"
        )
        sql: str = Field(
            description="one read-only SELECT returning game_id, team and "
            "numeric metric columns"
        )

    tried = _already_tried()
    prompt = [SCHEMA_NOTE]
    if tried:
        prompt.append("Already tried — propose something genuinely different:\n" + "\n".join(tried))
    if hint:
        prompt.append(f"The operator suggests exploring: {hint}")
    prompt.append("Propose one hypothesis.")

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    model_id = model or cfg.research_model
    response = client.messages.parse(
        model=model_id,
        max_tokens=16000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": "\n\n".join(prompt)}],
        output_format=Proposal,
    )
    parsed = response.parsed_output
    log.info("Proposed %r from %s", parsed.name, model_id)
    return Hypothesis(
        name=parsed.name,
        mechanism=parsed.mechanism,
        expected_sign=parsed.expected_sign,
        sql=parsed.sql,
        source=model_id,
    )
