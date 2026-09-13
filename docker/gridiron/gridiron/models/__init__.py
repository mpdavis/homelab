"""Spread models — the pluggable part.

A model turns "every game that had finished by ``as_of``" into a predicted home
margin for a set of upcoming games. That is the whole contract:

    model.fit(history, as_of)      -> None
    model.predict(fixtures)        -> np.ndarray of predicted home margins

Both frames are one row per game, from the home team's perspective, as built by
``gridiron.backtest.load_frame``. They carry the same columns, so a model can
be written against one shape:

    game_id, season, week, kickoff, home_team, away_team, neutral_site,
    margin, efficiency_margin, fp_margin_pts, market_margin, prestige_gap

``margin`` and its decomposition are NULL in ``fixtures`` — the game has not
been played. ``market_margin`` and ``prestige_gap`` are populated in both,
because both are known before kickoff.

A model is handed the whole of ``history`` and is trusted to respect ``as_of``
itself, because some models want to weight by age rather than filter by it.
Every shipped model filters on it first thing, and ``tests/test_backtest.py``
asserts that a model which cheats produces a visibly impossible result.

Registering a new theory is one decorator::

    @register_model("my_theory")
    class MyTheory(SpreadModel):
        def fit(self, history, as_of): ...
        def predict(self, fixtures): ...

and it is immediately available to ``gridiron backtest --model my_theory``,
to the sweep, and to the live edge report.
"""

from __future__ import annotations

from typing import Callable, Protocol

import numpy as np
import pandas as pd

from .weighting import Sweep, Weighting

__all__ = [
    "SpreadModel",
    "Sweep",
    "Weighting",
    "build_model",
    "model_names",
    "neutral_mask",
    "register_model",
    "registered_models",
]


def neutral_mask(fixtures: pd.DataFrame) -> np.ndarray:
    """``neutral_site`` as a plain boolean array, missing values read as False.

    DuckDB hands back a nullable boolean column as ``object`` dtype, and
    filling that in place downcasts it — deprecated in pandas, and a warning
    the test suite is configured to treat as an error. Every model needs the
    same three lines, so they live here once.
    """
    column = fixtures.get("neutral_site")
    if column is None:
        return np.zeros(len(fixtures), dtype=bool)
    return np.where(pd.isna(column), False, column).astype(bool)


class SpreadModel(Protocol):
    """What every model must provide."""

    name: str

    def fit(self, history: pd.DataFrame, as_of) -> None:
        """Learn from games that finished strictly before ``as_of``."""

    def predict(self, fixtures: pd.DataFrame) -> np.ndarray:
        """Predicted home margin, one per row of ``fixtures``."""


_REGISTRY: dict[str, Callable[..., SpreadModel]] = {}
_DESCRIPTIONS: dict[str, str] = {}


def register_model(name: str, description: str = ""):
    def decorator(factory):
        if name in _REGISTRY:
            raise ValueError(f"Model {name!r} is already registered")
        _REGISTRY[name] = factory
        _DESCRIPTIONS[name] = (
            description or (factory.__doc__ or "").strip().split("\n")[0]
        )
        return factory

    return decorator


def _load() -> None:
    # Import for side effects; each module registers its models.
    from . import baselines, ratings  # noqa: F401


def registered_models() -> dict[str, str]:
    _load()
    return dict(_DESCRIPTIONS)


def model_names() -> list[str]:
    return sorted(registered_models())


def build_model(name: str, **params) -> SpreadModel:
    _load()
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown model {name!r}. Registered: {', '.join(model_names())}"
        )
    return _REGISTRY[name](**params)
