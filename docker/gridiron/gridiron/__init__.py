"""Gridiron — college football betting research.

SIGN CONVENTIONS. Every quantity in this package that has a direction is
expressed as a *home margin in points*, positive when the home team is better
off. Read this once and the rest of the code stops being ambiguous.

    actual_margin  = home_points - away_points
    model_margin   = the model's prediction of that same quantity
    market_margin  = -spread

That last line is the one that bites people. Sportsbooks quote a spread from
the home team's perspective with the *favorite negative*: Alabama -14 at home
is ``spread = -14``, and it means the market expects a home margin of +14. So
market_margin is the negation of the quoted spread, always, everywhere.

    edge = model_margin - market_margin

A positive edge means the model likes the home team more than the market does,
so the bet is the HOME side. A negative edge is the away side. Nothing in this
package ever stores a raw spread outside of the ``lines`` and ``live_odds``
tables, which hold what the book actually published.

For a neutral-site game "home" is whichever team the data source lists first;
the home-field advantage term is zeroed out for those, so the convention still
holds and only the label is arbitrary.

POINT-IN-TIME DISCIPLINE. A backtest that trains on a game it is about to
predict will report a wonderful, entirely fictional edge. Leakage is the
default failure mode of this kind of system, so it is prevented structurally
rather than by care: every function that produces training data takes an
``as_of`` timestamp and is required to consider only games that had *finished*
before it. See ``gridiron.backtest`` and the leakage tests in
``tests/test_backtest.py``.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
