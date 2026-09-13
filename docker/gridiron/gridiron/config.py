"""Environment-driven configuration.

Everything is read once at import of :func:`settings` and cached, so a process
sees a stable view. Nothing here has a secret as a default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    # --- storage -----------------------------------------------------------
    data_dir: Path = Path("/data")
    db_filename: str = "gridiron.duckdb"

    # --- CollegeFootballData (history: games, plays, drives, closing lines) -
    cfbd_api_key: str = ""
    cfbd_base_url: str = "https://api.collegefootballdata.com"

    # --- The Odds API (live prices from the books you actually bet) ---------
    odds_api_key: str = ""
    odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    # Free tiers meter by request*market*region, so the default asks for only
    # the books that matter here rather than the whole US market.
    odds_books: list[str] = field(default_factory=lambda: ["fanatics", "draftkings"])

    # --- ingest ------------------------------------------------------------
    # 2015 is a deliberate floor: CFBD's play-by-play gets materially more
    # complete from the mid-2010s, and drive start position — the input to
    # every field-position feature — is patchy before it.
    first_season: int = 2015
    last_season: int = 0  # 0 means "current season, derived from the clock"

    # --- modelling defaults (every one of these is sweepable) --------------
    default_model: str = "decomposed"
    # Recency as a half-life in days rather than a window in games. A season is
    # ~120 days, so 240 says "two seasons ago counts a quarter as much as
    # yesterday" — smooth, and it never throws data away at a cliff edge.
    default_half_life_days: float = 240.0
    default_ridge_lambda: float = 12.0
    # Points of edge required before a game is called a bet. 2.5 is roughly a
    # key-number's worth and keeps the bet count honest.
    default_edge_threshold: float = 2.5

    # --- automated research -------------------------------------------------
    # Seasons from here on are the holdout: the search never sees them, and a
    # finalist gets exactly one look. Everything the search touches is spent
    # data, so this line is the only real defence against a strategy that is
    # just the best of N coin flips.
    holdout_from_season: int = 2024
    # Anthropic key for the hypothesis proposer. Absent, `research propose`
    # refuses and the rest of the harness still works by hand.
    anthropic_api_key: str = ""
    research_model: str = "claude-opus-5"

    # --- web ---------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    timezone: str = "America/Chicago"

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def has_cfbd(self) -> bool:
        return bool(self.cfbd_api_key)

    @property
    def has_odds_api(self) -> bool:
        return bool(self.odds_api_key)


@lru_cache(maxsize=1)
def settings() -> Settings:
    """The process-wide settings, resolved from the environment."""
    return Settings(
        data_dir=Path(os.environ.get("GRIDIRON_DATA_DIR", "/data")),
        db_filename=os.environ.get("GRIDIRON_DB_FILENAME", "gridiron.duckdb"),
        cfbd_api_key=os.environ.get("GRIDIRON_CFBD_API_KEY", ""),
        cfbd_base_url=os.environ.get(
            "GRIDIRON_CFBD_BASE_URL", "https://api.collegefootballdata.com"
        ),
        odds_api_key=os.environ.get("GRIDIRON_ODDS_API_KEY", ""),
        odds_api_base_url=os.environ.get(
            "GRIDIRON_ODDS_API_BASE_URL", "https://api.the-odds-api.com/v4"
        ),
        odds_books=_list("GRIDIRON_ODDS_BOOKS", ["fanatics", "draftkings"]),
        first_season=_int("GRIDIRON_FIRST_SEASON", 2015),
        last_season=_int("GRIDIRON_LAST_SEASON", 0),
        default_model=os.environ.get("GRIDIRON_DEFAULT_MODEL", "decomposed"),
        default_half_life_days=_float("GRIDIRON_HALF_LIFE_DAYS", 240.0),
        default_ridge_lambda=_float("GRIDIRON_RIDGE_LAMBDA", 12.0),
        default_edge_threshold=_float("GRIDIRON_EDGE_THRESHOLD", 2.5),
        holdout_from_season=_int("GRIDIRON_HOLDOUT_FROM_SEASON", 2024),
        anthropic_api_key=os.environ.get("GRIDIRON_ANTHROPIC_API_KEY", ""),
        research_model=os.environ.get("GRIDIRON_RESEARCH_MODEL", "claude-opus-5"),
        host=os.environ.get("GRIDIRON_HOST", "0.0.0.0"),
        port=_int("GRIDIRON_PORT", 8080),
        timezone=os.environ.get("TZ", "America/Chicago"),
    )


def current_season(today=None) -> int:
    """The season a date falls in.

    A college football season is named for the calendar year it kicks off in,
    and it runs into January. So anything before February belongs to the
    previous year's season — otherwise a January bowl game would be filed under
    a season that has not started yet.
    """
    from datetime import date

    today = today or date.today()
    return today.year - 1 if today.month < 2 else today.year


def season_range() -> tuple[int, int]:
    """The configured span of seasons to ingest, resolving 0 to the present."""
    cfg = settings()
    last = cfg.last_season or current_season()
    return cfg.first_season, last
