"""Feature blocks — where a theory becomes a column.

A *block* is one function that turns the source tables into a DataFrame keyed
by ``(game_id, team)``. ``build_team_game`` runs every registered block and
joins the results into the ``team_game`` table, which is the only thing the
models read.

Adding a theory is therefore one function and one decorator::

    @feature_block("my_theory", columns=["my_metric"])
    def my_theory(conn, seasons):
        return conn.execute("SELECT game_id, team, ... AS my_metric FROM ...").df()

then add ``my_metric`` to the ``team_game`` DDL in :mod:`gridiron.db` and name
it in a model's ``covariates``. Nothing else in the package needs to change.

The blocks that ship are in :mod:`gridiron.features.build`; season-level
prestige and portal metrics, which are keyed by ``(season, team)`` rather than
by game, live in :mod:`gridiron.features.prestige`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd

BlockFn = Callable[..., "pd.DataFrame"]


@dataclass(frozen=True)
class Block:
    name: str
    fn: BlockFn
    columns: tuple[str, ...]
    description: str


_REGISTRY: dict[str, Block] = {}


def feature_block(
    name: str, *, columns: Iterable[str], description: str = ""
) -> Callable[[BlockFn], BlockFn]:
    """Register a feature block under ``name``."""

    def decorator(fn: BlockFn) -> BlockFn:
        if name in _REGISTRY:
            raise ValueError(f"Feature block {name!r} is already registered")
        _REGISTRY[name] = Block(
            name=name,
            fn=fn,
            columns=tuple(columns),
            description=description or (fn.__doc__ or "").strip().split("\n")[0],
        )
        return fn

    return decorator


def registered_blocks() -> dict[str, Block]:
    # Importing the module is what populates the registry; doing it lazily here
    # keeps `gridiron.features` importable from inside `build` itself.
    from . import build  # noqa: F401

    return dict(_REGISTRY)


def block_columns() -> list[str]:
    """Every column contributed by every registered block."""
    seen: list[str] = []
    for block in registered_blocks().values():
        for column in block.columns:
            if column not in seen:
                seen.append(column)
    return seen
