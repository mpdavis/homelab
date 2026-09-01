"""Upstream data sources.

Two of them, for two different jobs:

* :mod:`gridiron.sources.cfbd` — CollegeFootballData. Games, play-by-play,
  drives, historical closing lines, talent and recruiting. This is what the
  backtester learns from.
* :mod:`gridiron.sources.oddsapi` — The Odds API. Live prices from the books
  actually being bet, Fanatics and DraftKings included. CFBD carries DraftKings
  historically but nothing from Fanatics and nothing in real time, so the two
  sources are complements rather than alternatives.
"""

from __future__ import annotations


def pick(payload: dict, *names: str, default=None):
    """Read the first key present out of several spellings.

    CFBD has migrated field names between snake_case and camelCase across API
    versions (``home_team`` vs ``homeTeam``), and it did not do so uniformly.
    Rather than pin a version and break on the next migration, every read goes
    through here with both spellings listed.
    """
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def as_int(value, default=None):
    """Coerce to int, tolerating the nulls and floats the APIs mix in."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "t", "1", "yes"}
    return bool(value)
