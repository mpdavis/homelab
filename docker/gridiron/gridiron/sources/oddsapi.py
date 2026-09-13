"""The Odds API client — live prices from the books you actually bet.

CFBD's historical ``/lines`` is what the backtester learns against, but it
carries no Fanatics and nothing in real time. This fills that gap: current
spreads, totals and moneylines for ``americanfootball_ncaaf``, filtered to the
books named in ``GRIDIRON_ODDS_BOOKS`` (Fanatics and DraftKings by default).

The free tier meters requests, and the meter counts *markets times regions* per
call, so a single request asking for three markets costs three credits. The
client surfaces the remaining quota from the response headers so the UI can
show it before it runs out mid-Saturday.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import settings
from . import as_float, as_int

log = logging.getLogger(__name__)

SPORT = "americanfootball_ncaaf"

# Book names as The Odds API keys them. Fanatics acquired PointsBet's US
# business and the feed has carried both keys through the transition, so both
# are accepted and normalised to one label.
BOOK_ALIASES = {
    "fanatics": "fanatics",
    "pointsbetus": "fanatics",
    "draftkings": "draftkings",
}

# Schools whose Odds API name does not reduce to the CFBD school name by
# stripping the mascot. Everything else is handled generically by
# TeamMatcher — this is only the genuinely irregular remainder.
NAME_OVERRIDES = {
    "ole miss rebels": "Ole Miss",
    "miami hurricanes": "Miami",
    "miami (oh) redhawks": "Miami (OH)",
    "miami redhawks": "Miami (OH)",
    "texas a&m aggies": "Texas A&M",
    "louisiana ragin cajuns": "Louisiana",
    "louisiana ragin' cajuns": "Louisiana",
    "louisiana lafayette ragin cajuns": "Louisiana",
    "ul monroe warhawks": "Louisiana Monroe",
    "louisiana monroe warhawks": "Louisiana Monroe",
    "usc trojans": "USC",
    "ucf knights": "UCF",
    "utep miners": "UTEP",
    "utsa roadrunners": "UTSA",
    "unlv rebels": "UNLV",
    "smu mustangs": "SMU",
    "tcu horned frogs": "TCU",
    "byu cougars": "BYU",
    "lsu tigers": "LSU",
    "nc state wolfpack": "NC State",
    "north carolina state wolfpack": "NC State",
    "san jose state spartans": "San José State",
    "hawaii rainbow warriors": "Hawai'i",
    "sam houston state bearkats": "Sam Houston",
    "appalachian state mountaineers": "Appalachian State",
}


class OddsAPIError(RuntimeError):
    pass


@dataclass
class Quota:
    """What the API says is left on the plan, from the response headers."""

    remaining: int | None = None
    used: int | None = None
    last_cost: int | None = None


def _normalise(name: str) -> str:
    """Lowercase, strip punctuation and accents-ish, collapse whitespace."""
    text = name.lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class TeamMatcher:
    """Maps Odds API team names onto CFBD school names.

    The Odds API gives a full brand name ("Alabama Crimson Tide"); CFBD keys
    everything on the school ("Alabama"). Rather than fuzzy-match — which
    silently mis-resolves "Miami" every year — this builds an exact lookup from
    the ``teams`` table, which carries both school and mascot, and only falls
    back to prefix matching for names the lookup misses.
    """

    def __init__(self, teams: list[tuple[str, str | None]]):
        self._exact: dict[str, str] = {}
        self._by_school: dict[str, str] = {}
        for school, mascot in teams:
            if not school:
                continue
            self._by_school[_normalise(school)] = school
            self._exact[_normalise(school)] = school
            if mascot:
                self._exact[_normalise(f"{school} {mascot}")] = school
        for alias, school in NAME_OVERRIDES.items():
            self._exact[_normalise(alias)] = school

    def resolve(self, name: str) -> str | None:
        """The CFBD school for an Odds API name, or None if it is unmatched.

        Returning None rather than guessing is deliberate: an unmatched team
        means a game silently gets no edge shown, which is a visible gap. A bad
        guess means an edge computed against the wrong team's rating, which is
        a confident wrong bet.
        """
        key = _normalise(name)
        if key in self._exact:
            return self._exact[key]
        # "Alabama Crimson Tide" -> longest school name that prefixes it.
        best: str | None = None
        for school_key, school in self._by_school.items():
            if key.startswith(school_key + " ") and (
                best is None or len(school_key) > len(_normalise(best))
            ):
                best = school
        if best is None:
            log.warning("Unmatched odds-api team name: %r", name)
        return best


class OddsAPIClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        cfg = settings()
        self.api_key = api_key if api_key is not None else cfg.odds_api_key
        self.base_url = (base_url or cfg.odds_api_base_url).rstrip("/")
        self.books = [b.lower() for b in cfg.odds_books]
        if not self.api_key:
            raise OddsAPIError(
                "No Odds API key. Set GRIDIRON_ODDS_API_KEY — the free tier at "
                "the-odds-api.com covers a season of weekly pulls."
            )
        self._client = httpx.Client(
            base_url=self.base_url, timeout=httpx.Timeout(30.0, connect=10.0)
        )
        self.quota = Quota()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OddsAPIClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        params["apiKey"] = self.api_key
        response = self._client.get(path, params=params)
        self.quota = Quota(
            remaining=as_int(response.headers.get("x-requests-remaining")),
            used=as_int(response.headers.get("x-requests-used")),
            last_cost=as_int(response.headers.get("x-requests-last")),
        )
        if response.status_code == 401:
            raise OddsAPIError("Odds API rejected the key (401)")
        if response.status_code == 429:
            raise OddsAPIError("Odds API quota exhausted (429)")
        if response.status_code != 200:
            raise OddsAPIError(
                f"Odds API {path} failed with {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response.json()

    def odds(self, matcher: TeamMatcher) -> list[dict]:
        """Current spreads, totals and moneylines, flattened for ``live_odds``.

        Team names are resolved to CFBD schools here rather than at query time,
        so everything downstream joins on one vocabulary. Events whose teams do
        not resolve are dropped with a warning rather than stored half-matched.
        """
        # Asking for the books by key rather than the whole US region keeps the
        # per-request credit cost down and the payload small.
        payload = self._get(
            f"/sports/{SPORT}/odds",
            regions="us",
            markets="spreads,h2h,totals",
            oddsFormat="american",
            bookmakers=",".join(self.books),
        )
        fetched_at = datetime.now(timezone.utc)
        rows: list[dict] = []
        for event in payload or []:
            home = matcher.resolve(event.get("home_team", "") or "")
            away = matcher.resolve(event.get("away_team", "") or "")
            if not home or not away:
                continue
            commence = _parse_ts(event.get("commence_time"))
            for bookmaker in event.get("bookmakers", []) or []:
                book = BOOK_ALIASES.get(
                    (bookmaker.get("key") or "").lower(), bookmaker.get("key")
                )
                for market in bookmaker.get("markets", []) or []:
                    market_key = market.get("key")
                    for outcome in market.get("outcomes", []) or []:
                        raw_name = outcome.get("name", "") or ""
                        # Totals name their sides Over/Under; spread and
                        # moneyline outcomes name a team, which needs the same
                        # normalisation as the event itself.
                        if market_key == "totals":
                            outcome_name = raw_name
                        else:
                            outcome_name = matcher.resolve(raw_name) or raw_name
                        rows.append(
                            {
                                "fetched_at": fetched_at,
                                "event_id": event.get("id"),
                                "commence_time": commence,
                                "home_team": home,
                                "away_team": away,
                                "book": book,
                                "market": market_key,
                                "outcome": outcome_name,
                                "price": as_int(outcome.get("price")),
                                "point": as_float(outcome.get("point")),
                            }
                        )
        return rows


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
