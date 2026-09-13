"""The trial ledger, the holdout, and the significance bar the count implies."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import NormalDist

from ..config import settings
from ..db import cursor

# Stages, cheapest first. The order is the point: persistence and market cost
# one scan each and kill most ideas, so they run before anything touches the
# backtester.
STAGES = ("persistence", "market", "backtest", "holdout")


def search_seasons(conn) -> list[int]:
    """Seasons the search is allowed to see."""
    cut = settings().holdout_from_season
    rows = conn.execute(
        "SELECT DISTINCT season FROM games WHERE season < ? ORDER BY season", [cut]
    ).fetchall()
    return [int(r[0]) for r in rows]


def holdout_seasons(conn) -> list[int]:
    """Seasons reserved for a finalist's single look."""
    cut = settings().holdout_from_season
    rows = conn.execute(
        "SELECT DISTINCT season FROM games WHERE season >= ? ORDER BY season", [cut]
    ).fetchall()
    return [int(r[0]) for r in rows]


class HoldoutViolation(RuntimeError):
    """Raised when a search-stage evaluation would touch reserved seasons."""


def guard_seasons(conn, seasons: list[int], *, stage: str) -> list[int]:
    """Refuse to evaluate reserved seasons outside the holdout stage.

    An exception rather than a silent filter. Quietly dropping the seasons
    would let a caller believe it had tested a decade when it had tested eight
    years, and the whole value of a holdout is knowing exactly when it was
    spent.
    """
    if stage == "holdout":
        return sorted(seasons)
    reserved = set(holdout_seasons(conn))
    trespass = sorted(set(seasons) & reserved)
    if trespass:
        raise HoldoutViolation(
            f"stage {stage!r} asked for holdout seasons {trespass}. Those are "
            f"reserved (GRIDIRON_HOLDOUT_FROM_SEASON="
            f"{settings().holdout_from_season}) and a finalist gets one look at "
            "them via the 'holdout' stage. Searching them spends them."
        )
    return sorted(seasons)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


@dataclass
class Hypothesis:
    name: str
    mechanism: str
    sql: str
    expected_sign: str = "positive"
    source: str = "human"
    hypothesis_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "proposed"


def record_hypothesis(hypothesis: Hypothesis) -> str:
    with cursor() as conn:
        conn.execute(
            """
            INSERT INTO research_hypotheses
                (hypothesis_id, created_at, name, mechanism, expected_sign,
                 sql, source, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                hypothesis.hypothesis_id,
                datetime.now(timezone.utc),
                hypothesis.name,
                hypothesis.mechanism,
                hypothesis.expected_sign,
                hypothesis.sql,
                hypothesis.source,
                hypothesis.status,
            ],
        )
    return hypothesis.hypothesis_id


def record_trial(
    hypothesis_id: str,
    stage: str,
    *,
    seasons: list[int],
    passed: bool,
    statistic: float | None,
    metrics: dict | None = None,
    note: str = "",
) -> str:
    trial_id = uuid.uuid4().hex[:12]
    with cursor() as conn:
        conn.execute(
            """
            INSERT INTO research_trials
                (trial_id, hypothesis_id, created_at, stage, seasons, passed,
                 statistic, metrics, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trial_id,
                hypothesis_id,
                datetime.now(timezone.utc),
                stage,
                ",".join(str(s) for s in seasons),
                passed,
                None if statistic is None else float(statistic),
                json.dumps(metrics or {}, default=str),
                note,
            ],
        )
    return trial_id


def set_status(hypothesis_id: str, status: str) -> None:
    with cursor() as conn:
        conn.execute(
            "UPDATE research_hypotheses SET status = ? WHERE hypothesis_id = ?",
            [status, hypothesis_id],
        )


def trial_count(stage: str | None = "backtest") -> int:
    """How many looks have been taken. The multiple-testing denominator.

    Counts backtest evaluations by default rather than every row, because the
    cheap filters are not where the selection happens — a hypothesis only gets
    to consume the sample once it reaches the backtester. Pass None to count
    everything.
    """
    with cursor() as conn:
        if stage is None:
            row = conn.execute("SELECT count(*) FROM research_trials").fetchone()
        else:
            row = conn.execute(
                "SELECT count(*) FROM research_trials WHERE stage = ?", [stage]
            ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# What the count does to significance
# ---------------------------------------------------------------------------


def expected_max_t(n_trials: int) -> float:
    """The largest |t| pure noise is expected to produce over n tries.

    sqrt(2 ln n) is the standard asymptotic for the maximum of n standard
    normals. It is the number to put next to a result before celebrating it.
    """
    n = max(int(n_trials), 1)
    if n == 1:
        return 0.0
    return math.sqrt(2.0 * math.log(n))


def required_t(n_trials: int, alpha: float = 0.05) -> float:
    """The |t| a candidate must clear after n looks, Šidák-corrected.

    Šidák rather than Bonferroni because the trials are not independent — the
    same seasons, often overlapping features — and Šidák is the slightly less
    punishing of the two while still controlling the family-wise error rate.
    Neither is exactly right for correlated tests; both are far better than
    pretending n is 1.
    """
    n = max(int(n_trials), 1)
    per_test = 1.0 - (1.0 - alpha) ** (1.0 / n)
    return NormalDist().inv_cdf(1.0 - per_test / 2.0)


def assess(t_statistic: float, *, n_trials: int | None = None, alpha: float = 0.05) -> dict:
    """Read a t-statistic in the light of how many times we have looked."""
    n = trial_count() if n_trials is None else n_trials
    bar = required_t(n, alpha)
    noise = expected_max_t(n)
    magnitude = abs(t_statistic)
    return {
        "t": round(float(t_statistic), 3),
        "trials_so_far": n,
        "required_t": round(bar, 3),
        "expected_max_t_under_null": round(noise, 3),
        "clears_corrected_bar": bool(magnitude >= bar),
        "verdict": (
            f"|t|={magnitude:.2f} clears the {bar:.2f} needed after {n} looks"
            if magnitude >= bar
            else f"|t|={magnitude:.2f} is short of the {bar:.2f} needed after "
            f"{n} looks — noise alone would be expected to reach {noise:.2f}"
        ),
    }


def summary() -> dict:
    """What the search has done so far, for the CLI and the status page."""
    with cursor() as conn:
        hypotheses = conn.execute(
            "SELECT status, count(*) FROM research_hypotheses GROUP BY status"
        ).fetchall()
        stages = conn.execute(
            "SELECT stage, count(*), sum(CASE WHEN passed THEN 1 ELSE 0 END) "
            "FROM research_trials GROUP BY stage"
        ).fetchall()
        search = search_seasons(conn)
        held = holdout_seasons(conn)
    n = trial_count()
    return {
        "by_status": {row[0]: int(row[1]) for row in hypotheses},
        "by_stage": {
            row[0]: {"trials": int(row[1]), "passed": int(row[2] or 0)} for row in stages
        },
        "search_seasons": search,
        "holdout_seasons": held,
        "backtest_trials": n,
        "required_t_now": round(required_t(n), 3),
        "expected_max_t_under_null": round(expected_max_t(n), 3),
    }
