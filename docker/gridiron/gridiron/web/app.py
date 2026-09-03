"""The web UI.

Read-only over the research the CLI produces, with one exception: the edge
table is computed on request, because it has to reflect the prices that are up
right now rather than the ones that were up when a job last ran.

Analyses are cached with a short TTL. Each one scans the whole history, which
takes a few seconds — fine on demand, not fine on every page load from a
browser someone left open on a second monitor.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import __version__, analysis, scheduler
from ..config import settings
from ..db import cursor, table_counts
from ..edges import EdgeConfig, best_price_per_game, compute_edges

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 300


def cached(key: str, producer: Callable[[], Any], ttl: int = CACHE_TTL_SECONDS) -> Any:
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = producer()
    _CACHE[key] = (now, value)
    return value


def _records(frame) -> list[dict]:
    if frame is None or len(frame) == 0:
        return []
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def create_app() -> FastAPI:
    app = FastAPI(title="Gridiron", version=__version__, docs_url="/api/docs")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Deliberately shallow: it answers "is this process serving", which is
        # what a liveness probe should restart on. Data staleness is a
        # different question and is on the status page, where a human can
        # judge it — restarting the pod would not fix a expired API key.
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        threshold: float = Query(default=0.0, ge=0.0, le=30.0),
        bankroll: float = Query(default=1000.0, gt=0),
        days: int = Query(default=10, ge=1, le=30),
    ):
        cfg = settings()
        effective = threshold or cfg.default_edge_threshold
        error = None
        best: list[dict] = []
        allrows: list[dict] = []
        try:
            edges = compute_edges(
                EdgeConfig(threshold=effective, bankroll=bankroll, days_ahead=days)
            )
            allrows = _records(edges)
            best = _records(best_price_per_game(edges))
        except Exception as exc:  # noqa: BLE001 — an empty store is the normal
            # first-run state, not a crash worth a 500.
            log.warning("Edge computation failed: %s", exc)
            error = str(exc)

        return TEMPLATES.TemplateResponse(
            request,
            "edges.html",
            {
                "title": "Edges",
                "bets": best,
                "all_rows": allrows,
                "threshold": effective,
                "bankroll": bankroll,
                "days": days,
                "error": error,
                "model": cfg.default_model,
            },
        )

    @app.get("/theories", response_class=HTMLResponse)
    def theories(request: Request):
        premium = cached("brand_premium", analysis.brand_premium)
        persistence = cached("persistence", analysis.hidden_yardage_persistence)
        market_test = cached("market_test", analysis.hidden_yardage_market_test)
        portal = cached("portal", lambda: _records(analysis.portal_and_prestige()))
        curve = cached("fp_curve", lambda: _records(analysis.fp_curve_table()))
        return TEMPLATES.TemplateResponse(
            request,
            "theories.html",
            {
                "title": "Theories",
                "premium": premium,
                "persistence": persistence,
                "market_test": market_test,
                "portal": portal,
                "curve": curve,
            },
        )

    @app.get("/backtests", response_class=HTMLResponse)
    def backtests(request: Request):
        with cursor() as conn:
            runs = conn.execute(
                """
                SELECT run_id, created_at, label, model, first_season,
                       last_season, metrics
                FROM backtest_runs
                ORDER BY created_at DESC
                LIMIT 100
                """
            ).df()
        rows = []
        for row in _records(runs):
            metrics = row.get("metrics")
            if isinstance(metrics, str):
                try:
                    metrics = json.loads(metrics)
                except json.JSONDecodeError:
                    metrics = {}
            rows.append({**row, "metrics": metrics or {}})
        return TEMPLATES.TemplateResponse(
            request, "backtests.html", {"title": "Backtests", "runs": rows}
        )

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request):
        cfg = settings()
        with cursor() as conn:
            coverage = conn.execute(
                """
                SELECT season,
                       count(*)                                   AS games,
                       count(*) FILTER (WHERE completed)          AS completed,
                       (SELECT count(*) FROM plays p WHERE p.season = g.season)  AS plays,
                       (SELECT count(*) FROM drives d WHERE d.season = g.season) AS drives
                FROM games g
                GROUP BY season
                ORDER BY season DESC
                """
            ).df()
        return TEMPLATES.TemplateResponse(
            request,
            "status.html",
            {
                "title": "Status",
                "counts": table_counts(),
                "coverage": _records(coverage),
                "refresh": scheduler.STATE.snapshot(),
                "cfbd": cfg.has_cfbd,
                "odds_api": cfg.has_odds_api,
                "books": cfg.odds_books,
                "db_path": str(cfg.db_path),
                "version": __version__,
            },
        )

    # -- JSON, for pulling numbers into a notebook -------------------------

    @app.get("/api/edges")
    def api_edges(threshold: float = 0.0, bankroll: float = 1000.0, days: int = 10):
        edges = compute_edges(
            EdgeConfig(
                threshold=threshold or settings().default_edge_threshold,
                bankroll=bankroll,
                days_ahead=days,
            )
        )
        return {
            "best": _records(best_price_per_game(edges)),
            "all": _records(edges),
        }

    @app.get("/api/theories/brand-premium")
    def api_brand_premium():
        return cached("brand_premium", analysis.brand_premium)

    @app.get("/api/theories/persistence")
    def api_persistence():
        return cached("persistence", analysis.hidden_yardage_persistence)

    @app.get("/api/theories/market-test")
    def api_market_test():
        return cached("market_test", analysis.hidden_yardage_market_test)

    @app.get("/api/status")
    def api_status():
        return {
            "version": __version__,
            "counts": table_counts(),
            "refresh": scheduler.STATE.snapshot(),
        }

    @app.post("/api/refresh")
    def api_refresh(
        seasons: str | None = Query(
            None, description='e.g. "2015-2026" or "2019,2021"; omitted = current season'
        ),
    ):
        """Run an ingest now, in this process.

        The only way to trigger one. DuckDB's lock is exclusive, so
        `kubectl exec ... gridiron ingest` cannot open the file the server is
        holding — a second writer is not a thing this design permits. Anything
        that needs to write has to be asked of the process that owns the file,
        which is what this endpoint is for.

        Runs on a worker thread so a decade-long backfill does not hold the
        request open for ten minutes. Poll /api/status for the outcome.
        """
        if scheduler.STATE.running:
            return JSONResponse(
                {"started": False, "reason": "a refresh is already running"},
                status_code=409,
            )
        parsed = _parse_seasons(seasons)
        threading.Thread(
            target=scheduler.refresh_data_once,
            args=(parsed,),
            name="gridiron-manual-refresh",
            daemon=True,
        ).start()
        return {
            "started": True,
            "seasons": parsed or "current season (or full backfill if empty)",
            "poll": "/api/status",
        }

    return app


def _parse_seasons(text: str | None) -> list[int] | None:
    """Accept "2019-2024", "2019,2021,2023" or a single year."""
    if not text:
        return None
    seasons: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            seasons.extend(range(int(start), int(end) + 1))
        else:
            seasons.append(int(part))
    return sorted(set(seasons)) or None
