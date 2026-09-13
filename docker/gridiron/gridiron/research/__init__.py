"""Automated hypothesis search, with the guardrails that make it mean anything.

The search itself is the easy half. Ask a model for a hundred ideas, run each
through the backtester, keep the best — and you will reliably produce a
strategy that looks profitable and is not. This is the central hazard, so it is
worth stating in numbers rather than as a caution.

Under a pure null, the largest |t| you expect from N independent tries is about
sqrt(2 ln N):

    20 tries    ->  2.4
    200 tries   ->  3.3
    2,000 tries ->  3.9

So a candidate showing t = 3.0 after two hundred attempts is not evidence of
anything. It is the *expected* outcome of attempting two hundred times. And the
bootstrap interval `gridiron backtest` reports assumes you asked once — running
a search silently invalidates the one honest number in the package.

Three things make it legitimate, and all three live in `registry`:

**A locked holdout.** Seasons from `holdout_from_season` on are never visible to
the search. Only a finalist is measured against them, once. Looking spends them.

**A trial count that cannot be pruned.** Every evaluation is a row in
`research_trials`, including the ones that failed and the ones that were re-run
with different parameters. The count is the denominator of every significance
claim made here, so deleting rows would quietly inflate all of them.

**A mechanism recorded before the result.** `propose` requires the model to say
why an effect should exist before it learns whether it does. This is the part a
grid search cannot do and the reason an LLM is worth involving at all: a
hypothesis with a reason attached is the kind that survives out of sample, and
one invented to fit is not.

The pipeline runs cheap filters first — is it a skill (`persistence`), is it
already priced (`market`) — because those cost nothing and kill most ideas
before they consume any of the sample.
"""

from __future__ import annotations

__all__ = ["registry", "sqlguard", "evaluate", "propose"]
