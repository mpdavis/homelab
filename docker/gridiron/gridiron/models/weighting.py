"""How much a game from a while ago should still count.

You asked for backtests over varying amounts of time rather than one fixed
window, and this is the module that makes that a swept parameter instead of a
constant buried in a query.

A hard window ("last 20 games") is the usual approach and it has an ugly
property: a game is worth full value on Friday and nothing on Saturday, so the
model lurches every week as good and bad games fall off the back. An
exponential half-life instead gives every game a weight that only ever decays
smoothly, which both predicts better and makes a *sweep* meaningful — the
half-life is a continuous knob, so plotting performance against it shows a
curve with a shape rather than a jagged step function.

Windows are still supported, because being able to show that the smooth
version beats the obvious version is worth more than asserting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# A college season is about four months of games followed by eight months of
# roster turnover. Weighting purely by elapsed days treats those eight months
# as ordinary time, which understates how much changes across an offseason —
# new coordinators, a graduating quarterback, a portal class. This multiplies
# the weight of anything from a previous season by an extra factor per season
# boundary crossed, on top of the day-based decay.
DEFAULT_SEASON_CARRYOVER = 0.65


@dataclass(frozen=True)
class Weighting:
    """A recency kernel, applied to training rows by age in days.

    ``kind``
        ``exponential`` — weight halves every ``half_life_days``. The default.
        ``window`` — every game inside ``window_days`` counts fully, everything
        older counts nothing. The naive baseline, kept for comparison.
        ``uniform`` — everything counts equally, the "all history" extreme.
    ``season_carryover``
        Extra multiplier per season boundary crossed. 1.0 disables it.
    ``max_age_days``
        Hard truncation on top of any kernel. Mostly a performance guard: at a
        240-day half-life a game from 2016 carries a weight of about 1e-5 and
        contributes nothing but a row to the design matrix.
    """

    kind: str = "exponential"
    half_life_days: float = 240.0
    window_days: float | None = None
    season_carryover: float = DEFAULT_SEASON_CARRYOVER
    max_age_days: float | None = 2000.0

    def weights(
        self, ages_days: np.ndarray, seasons_back: np.ndarray | None = None
    ) -> np.ndarray:
        """Weights for training rows of the given ages, in the same order.

        ``seasons_back`` is how many season boundaries each row sits behind the
        prediction; pass None to skip the carryover term.
        """
        ages = np.asarray(ages_days, dtype=float)
        # A negative age means the row is in the future relative to the cutoff.
        # That is a leak, and it is the caller's bug, but zeroing it here means
        # the bug shows up as a useless model rather than a brilliant one.
        ages = np.where(ages < 0, np.inf, ages)

        if self.kind == "uniform":
            out = np.ones_like(ages)
        elif self.kind == "window":
            span = self.window_days if self.window_days is not None else 365.0
            out = (ages <= span).astype(float)
        elif self.kind == "exponential":
            if self.half_life_days <= 0:
                raise ValueError("half_life_days must be positive")
            out = np.power(0.5, ages / self.half_life_days)
        else:
            raise ValueError(f"Unknown weighting kind {self.kind!r}")

        if self.max_age_days is not None:
            out = np.where(ages > self.max_age_days, 0.0, out)

        if seasons_back is not None and self.season_carryover != 1.0:
            back = np.clip(np.asarray(seasons_back, dtype=float), 0, None)
            out = out * np.power(self.season_carryover, back)

        return np.where(np.isfinite(out), out, 0.0)

    def describe(self) -> str:
        if self.kind == "exponential":
            base = f"exp(half-life {self.half_life_days:g}d)"
        elif self.kind == "window":
            base = f"window({self.window_days:g}d)"
        else:
            base = "uniform"
        if self.season_carryover != 1.0:
            base += f" x{self.season_carryover:g}/season"
        return base

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "half_life_days": self.half_life_days,
            "window_days": self.window_days,
            "season_carryover": self.season_carryover,
            "max_age_days": self.max_age_days,
        }


@dataclass(frozen=True)
class Sweep:
    """A set of weightings to compare in one backtest run.

    The default ladder spans "this season only" to "four years of history",
    which is the range over which the answer plausibly lives. Anything shorter
    than about 60 days is fewer than ten games and is dominated by noise;
    anything longer than about 1000 is indistinguishable from uniform.
    """

    half_lives: tuple[float, ...] = (60, 90, 120, 180, 240, 365, 550, 800)
    include_windows: bool = True
    window_days: tuple[float, ...] = field(default=(120, 365, 730))
    season_carryover: float = DEFAULT_SEASON_CARRYOVER

    def weightings(self) -> list[Weighting]:
        out = [
            Weighting(
                kind="exponential",
                half_life_days=half_life,
                season_carryover=self.season_carryover,
            )
            for half_life in self.half_lives
        ]
        if self.include_windows:
            out.extend(
                Weighting(
                    kind="window",
                    window_days=days,
                    season_carryover=self.season_carryover,
                )
                for days in self.window_days
            )
            out.append(Weighting(kind="uniform", season_carryover=self.season_carryover))
        return out
