"""CollegeFootballData API client.

Free with an API key from collegefootballdata.com. It is the only public source
that carries play-by-play, drive start position and historical closing lines
together, which is what makes the whole "hidden yardage" line of enquiry
possible at all — you cannot recover a team's average starting field position
from a box score.

Every method returns plain dicts shaped for the tables in :mod:`gridiron.db`;
nothing upstream of ingest sees a raw API payload.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from ..config import settings
from . import as_bool, as_float, as_int, pick

log = logging.getLogger(__name__)

# Regular season plus postseason. CFBD splits them, and bowl games are both a
# meaningful chunk of the betting calendar and the games where the market is
# least efficient, so they are never skipped.
SEASON_TYPES = ("regular", "postseason")

# CFBD's play endpoint requires a week. Regular seasons run to 15 with the
# conference championship weekend; asking past the end is a cheap empty list.
MAX_REGULAR_WEEK = 16


class CFBDError(RuntimeError):
    pass


class CFBDClient:
    """A thin, retrying wrapper over the CFBD REST API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        cfg = settings()
        self.api_key = api_key if api_key is not None else cfg.cfbd_api_key
        self.base_url = (base_url or cfg.cfbd_base_url).rstrip("/")
        if not self.api_key:
            raise CFBDError(
                "No CFBD API key. Set GRIDIRON_CFBD_API_KEY — a free key comes "
                "from collegefootballdata.com/key."
            )
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CFBDClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def get(self, path: str, **params: Any) -> list[dict]:
        """GET a list endpoint, retrying transient failures.

        CFBD rate-limits generously but not infinitely, and a full historical
        backfill is thousands of calls. 429 and 5xx back off; everything else
        raises immediately, because a 401 will not get better by waiting.
        """
        params = {k: v for k, v in params.items() if v is not None}
        delay = 2.0
        for attempt in range(5):
            response = self._client.get(path, params=params)
            if response.status_code == 200:
                payload = response.json()
                return payload if isinstance(payload, list) else [payload]
            if response.status_code in (429, 500, 502, 503, 504):
                log.warning(
                    "CFBD %s returned %s (attempt %d/5); backing off %.0fs",
                    path,
                    response.status_code,
                    attempt + 1,
                    delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise CFBDError(
                f"CFBD {path} failed with {response.status_code}: {response.text[:400]}"
            )
        raise CFBDError(f"CFBD {path} still failing after 5 attempts")

    # -- endpoints ---------------------------------------------------------

    def teams(self, season: int) -> list[dict]:
        """FBS teams for a season.

        The mascot is pulled through because it is what makes live odds
        matchable: The Odds API says "Alabama Crimson Tide" where CFBD says
        "Alabama", and school-plus-mascot bridges the two without fuzzy string
        matching. See :mod:`gridiron.sources.oddsapi`.
        """
        rows = self.get("/teams/fbs", year=season)
        return [
            {
                "school": pick(row, "school", "team"),
                "mascot": pick(row, "mascot"),
                "conference": pick(row, "conference"),
                "classification": pick(row, "classification", default="fbs"),
            }
            for row in rows
            if pick(row, "school", "team")
        ]

    def games(self, season: int) -> list[dict]:
        out: list[dict] = []
        for season_type in SEASON_TYPES:
            for row in self.get("/games", year=season, seasonType=season_type):
                game_id = as_int(pick(row, "id", "gameId", "game_id"))
                if game_id is None:
                    continue
                out.append(
                    {
                        "game_id": game_id,
                        "season": as_int(pick(row, "season"), season),
                        "week": as_int(pick(row, "week")),
                        "season_type": pick(
                            row, "seasonType", "season_type", default=season_type
                        ),
                        "start_date": _parse_ts(
                            pick(row, "startDate", "start_date")
                        ),
                        "neutral_site": as_bool(
                            pick(row, "neutralSite", "neutral_site")
                        ),
                        "conference_game": as_bool(
                            pick(row, "conferenceGame", "conference_game")
                        ),
                        "completed": as_bool(pick(row, "completed")),
                        "home_team": pick(row, "homeTeam", "home_team"),
                        "away_team": pick(row, "awayTeam", "away_team"),
                        "home_conference": pick(
                            row, "homeConference", "home_conference"
                        ),
                        "away_conference": pick(
                            row, "awayConference", "away_conference"
                        ),
                        "home_classification": pick(
                            row, "homeClassification", "home_classification"
                        ),
                        "away_classification": pick(
                            row, "awayClassification", "away_classification"
                        ),
                        "home_points": as_int(pick(row, "homePoints", "home_points")),
                        "away_points": as_int(pick(row, "awayPoints", "away_points")),
                        "venue": pick(row, "venue"),
                    }
                )
        return out

    def drives(self, season: int) -> list[dict]:
        out: list[dict] = []
        for season_type in SEASON_TYPES:
            for row in self.get("/drives", year=season, seasonType=season_type):
                drive_id = pick(row, "id", "driveId", "drive_id")
                if drive_id is None:
                    continue
                out.append(
                    {
                        "drive_id": str(drive_id),
                        "game_id": as_int(pick(row, "gameId", "game_id")),
                        "season": season,
                        "offense": pick(row, "offense"),
                        "defense": pick(row, "defense"),
                        "drive_number": as_int(
                            pick(row, "driveNumber", "drive_number")
                        ),
                        "start_period": as_int(
                            pick(row, "startPeriod", "start_period")
                        ),
                        "start_yards_to_goal": as_int(
                            pick(row, "startYardsToGoal", "start_yards_to_goal")
                        ),
                        "end_yards_to_goal": as_int(
                            pick(row, "endYardsToGoal", "end_yards_to_goal")
                        ),
                        "plays": as_int(pick(row, "plays")),
                        "yards": as_int(pick(row, "yards")),
                        "drive_result": pick(row, "driveResult", "drive_result"),
                        "scoring": as_bool(pick(row, "scoring")),
                        "start_offense_score": as_int(
                            pick(row, "startOffenseScore", "start_offense_score")
                        ),
                        "start_defense_score": as_int(
                            pick(row, "startDefenseScore", "start_defense_score")
                        ),
                        "end_offense_score": as_int(
                            pick(row, "endOffenseScore", "end_offense_score")
                        ),
                        "end_defense_score": as_int(
                            pick(row, "endDefenseScore", "end_defense_score")
                        ),
                    }
                )
        return out

    def plays(self, season: int, weeks: Iterable[int] | None = None) -> list[dict]:
        """Play-by-play for a season.

        The endpoint is per-week, so this is the expensive call in a backfill:
        roughly 17 requests a season, each a few megabytes. It is also the one
        that matters most — stuff rate, salvage yardage and success rate all
        come from here.
        """
        out: list[dict] = []
        week_list = list(weeks) if weeks is not None else range(1, MAX_REGULAR_WEEK + 1)
        for season_type in SEASON_TYPES:
            # The postseason is a single bucket rather than a run of weeks.
            iter_weeks = week_list if season_type == "regular" else [1]
            for week in iter_weeks:
                rows = self.get(
                    "/plays", year=season, week=week, seasonType=season_type
                )
                for row in rows:
                    play_id = pick(row, "id", "playId", "play_id")
                    if play_id is None:
                        continue
                    out.append(
                        {
                            "play_id": str(play_id),
                            "game_id": as_int(pick(row, "gameId", "game_id")),
                            "drive_id": _opt_str(pick(row, "driveId", "drive_id")),
                            "season": season,
                            "offense": pick(row, "offense"),
                            "defense": pick(row, "defense"),
                            "period": as_int(pick(row, "period")),
                            "down": as_int(pick(row, "down")),
                            "distance": as_int(pick(row, "distance")),
                            "yards_to_goal": as_int(
                                pick(row, "yardsToGoal", "yards_to_goal")
                            ),
                            "yards_gained": as_int(
                                pick(row, "yardsGained", "yards_gained")
                            ),
                            "play_type": pick(row, "playType", "play_type"),
                            "scoring": as_bool(pick(row, "scoring")),
                            "ppa": as_float(pick(row, "ppa")),
                        }
                    )
        return out

    def lines(self, season: int) -> list[dict]:
        """Historical betting lines, flattened to one row per game per book.

        CFBD nests a list of providers under each game. Consensus is kept
        alongside the individual books: it is the most complete series and so
        the most honest thing to backtest against, while DraftKings is the one
        that can be compared with what you would actually have been offered.
        """
        out: list[dict] = []
        for season_type in SEASON_TYPES:
            for row in self.get("/lines", year=season, seasonType=season_type):
                game_id = as_int(pick(row, "id", "gameId", "game_id"))
                if game_id is None:
                    continue
                for book in pick(row, "lines", default=[]) or []:
                    provider = pick(book, "provider")
                    if not provider:
                        continue
                    out.append(
                        {
                            "game_id": game_id,
                            "provider": provider,
                            "spread": as_float(pick(book, "spread")),
                            "spread_open": as_float(
                                pick(book, "spreadOpen", "spread_open")
                            ),
                            "over_under": as_float(
                                pick(book, "overUnder", "over_under")
                            ),
                            "over_under_open": as_float(
                                pick(book, "overUnderOpen", "over_under_open")
                            ),
                            "home_moneyline": as_int(
                                pick(book, "homeMoneyline", "home_moneyline")
                            ),
                            "away_moneyline": as_int(
                                pick(book, "awayMoneyline", "away_moneyline")
                            ),
                        }
                    )
        # A game can list the same provider twice when CFBD merges feeds; the
        # table's primary key would reject the batch, so keep the last.
        deduped: dict[tuple[int, str], dict] = {}
        for row in out:
            deduped[(row["game_id"], row["provider"])] = row
        return list(deduped.values())

    def talent(self, season: int) -> list[dict]:
        return [
            {
                "season": as_int(pick(row, "year", "season"), season),
                "school": pick(row, "school", "team"),
                "talent": as_float(pick(row, "talent")),
            }
            for row in self.get("/talent", year=season)
            if pick(row, "school", "team")
        ]

    def recruiting(self, season: int) -> list[dict]:
        return [
            {
                "season": as_int(pick(row, "year", "season"), season),
                "school": pick(row, "team", "school"),
                "rank": as_int(pick(row, "rank")),
                "points": as_float(pick(row, "points")),
            }
            for row in self.get("/recruiting/teams", year=season)
            if pick(row, "team", "school")
        ]

    def portal(self, season: int) -> list[dict]:
        """Transfer portal movements for a season.

        Newer than the rest of CFBD — coverage starts around 2021, which is
        also when the thesis this feeds starts to matter. An empty list for an
        older season is expected, not an error.
        """
        try:
            rows = self.get("/player/portal", year=season)
        except CFBDError as exc:
            log.warning("Portal data unavailable for %s: %s", season, exc)
            return []
        return [
            {
                "season": as_int(pick(row, "season", "year"), season),
                "first_name": pick(row, "firstName", "first_name"),
                "last_name": pick(row, "lastName", "last_name"),
                "position": pick(row, "position"),
                "origin": pick(row, "origin"),
                "destination": pick(row, "destination"),
                "transfer_date": _parse_ts(
                    pick(row, "transferDate", "transfer_date")
                ),
                "rating": as_float(pick(row, "rating")),
                "stars": as_int(pick(row, "stars")),
                "eligibility": pick(row, "eligibility"),
            }
            for row in rows
        ]


def _opt_str(value) -> str | None:
    return None if value is None else str(value)


def _parse_ts(value) -> datetime | None:
    """Parse CFBD's ISO-8601 stamps into aware UTC datetimes.

    Kickoff time is load-bearing: it is the cutoff a backtest trains up to. A
    naive datetime here would silently compare against a naive `now` somewhere
    else and drift by the UTC offset, which is enough to leak a Saturday
    afternoon game into its own training set.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
