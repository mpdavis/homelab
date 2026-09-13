"""Seeding and the single-writer constraint.

The service can only be written to by the process that holds the database
file. That is a deliberate consequence of the storage engine, but it means the
usual operational escape hatch — shell into the pod and run the CLI — is not
available, and the seeding path has to work without it. These tests pin that
down, because getting it wrong leaves a service that starts cleanly, reports
healthy, and holds no data.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

import synth

from gridiron import db as db_module
from gridiron import scheduler
from gridiron.config import settings


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def test_a_second_process_cannot_open_the_database(conn):
    """The constraint every other decision here follows from.

    Spawned as a real subprocess rather than mocked: the lock is enforced by
    DuckDB against the OS, and a test that stubbed it would pass while the
    deployment it describes did not work.
    """
    path = settings().db_path
    conn.execute("CREATE TABLE IF NOT EXISTS lock_probe (x INTEGER)")

    script = textwrap.dedent(
        f"""
        import duckdb
        for kwargs in ({{}}, {{"read_only": True}}):
            try:
                duckdb.connect({str(path)!r}, **kwargs)
                print("OPENED", kwargs)
            except Exception as exc:
                print("REFUSED", type(exc).__name__)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    # Both attempts must be refused — read-only is not a way around it.
    assert result.stdout.count("REFUSED") == 2, result.stdout
    assert "OPENED" not in result.stdout


def test_the_lock_error_explains_where_the_work_has_to_go(conn, monkeypatch):
    """A raw IOException names a pid and helps nobody."""
    import duckdb

    def refuse(*_args, **_kwargs):
        raise duckdb.IOException(
            'IO Error: Could not set lock on file "x": Conflicting lock is held'
        )

    monkeypatch.setattr(duckdb, "connect", refuse)
    with pytest.raises(db_module.DatabaseLocked) as excinfo:
        db_module._open(settings().db_path)
    message = str(excinfo.value)
    assert "/api/refresh" in message
    assert "read-only" in message


def test_an_unrelated_io_error_is_not_relabelled(conn, monkeypatch):
    import duckdb

    def refuse(*_args, **_kwargs):
        raise duckdb.IOException("IO Error: disk is on fire")

    monkeypatch.setattr(duckdb, "connect", refuse)
    with pytest.raises(duckdb.IOException):
        db_module._open(settings().db_path)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_an_empty_database_backfills_the_whole_configured_range(conn, monkeypatch):
    """The bug this exists to prevent: a service that only ever holds one
    season, so every backtest has nothing to train on."""
    monkeypatch.setenv("GRIDIRON_FIRST_SEASON", "2019")
    monkeypatch.setenv("GRIDIRON_LAST_SEASON", "2023")
    settings.cache_clear()

    seen: list[list[int] | None] = []
    monkeypatch.setattr(
        scheduler, "ingest_seasons", lambda s: seen.append(s), raising=False
    )
    _run_refresh(monkeypatch, seen)

    assert seen and seen[0] == [2019, 2020, 2021, 2022, 2023]


def test_a_populated_database_refreshes_only_the_current_season(conn, monkeypatch):
    synth.build(conn)
    monkeypatch.setenv("GRIDIRON_FIRST_SEASON", "2019")
    monkeypatch.setenv("GRIDIRON_LAST_SEASON", "2023")
    settings.cache_clear()

    seen: list[list[int] | None] = []
    _run_refresh(monkeypatch, seen)

    assert seen and len(seen[0]) == 1


def test_explicit_seasons_win_over_both(conn, monkeypatch):
    seen: list[list[int] | None] = []
    _run_refresh(monkeypatch, seen, seasons=[2021])
    assert seen and seen[0] == [2021]


def _run_refresh(monkeypatch, seen, seasons=None):
    """Drive refresh_data_once with ingest and feature-build stubbed out."""
    from gridiron.ingest import IngestReport

    def fake_ingest(requested):
        seen.append(requested)
        return IngestReport(seasons=list(requested or []))

    monkeypatch.setattr("gridiron.ingest.ingest_seasons", fake_ingest)
    monkeypatch.setattr(
        "gridiron.features.build.build_team_game", lambda conn, seasons: 0
    )
    scheduler.refresh_data_once(seasons)


def test_a_failed_refresh_is_recorded_rather_than_raised(conn, monkeypatch):
    """The thread has to survive, and the failure has to become visible."""

    def explode(_requested):
        raise RuntimeError("CFBD said no")

    monkeypatch.setattr("gridiron.ingest.ingest_seasons", explode)
    scheduler.STATE.last_error = ""
    scheduler.refresh_data_once([2023])

    assert "CFBD said no" in scheduler.STATE.last_error
    assert scheduler.STATE.running is False
    assert scheduler.STATE.snapshot()["last_error"]


# ---------------------------------------------------------------------------
# The trigger endpoint
# ---------------------------------------------------------------------------


def _client(conn):
    from fastapi.testclient import TestClient

    from gridiron.web.app import create_app

    return TestClient(create_app())


def test_refresh_endpoint_starts_a_run_and_says_where_to_look(conn, monkeypatch):
    calls: list = []
    monkeypatch.setattr(scheduler, "refresh_data_once", lambda s=None: calls.append(s))

    response = _client(conn).post("/api/refresh?seasons=2019-2021")
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["poll"] == "/api/status"

    for _ in range(200):
        if calls:
            break
        import time

        time.sleep(0.01)
    assert calls == [[2019, 2020, 2021]]


def test_refresh_endpoint_refuses_to_stack_runs(conn, monkeypatch):
    monkeypatch.setattr(scheduler.STATE, "running", True)
    response = _client(conn).post("/api/refresh")
    assert response.status_code == 409
    assert response.json()["started"] is False


def test_refresh_endpoint_without_seasons_defers_to_the_scheduler(conn, monkeypatch):
    calls: list = []
    monkeypatch.setattr(scheduler, "refresh_data_once", lambda s=None: calls.append(s))
    response = _client(conn).post("/api/refresh")
    assert response.status_code == 200

    for _ in range(200):
        if calls:
            break
        import time

        time.sleep(0.01)
    # None, so refresh_data_once applies its own empty-vs-populated rule.
    assert calls == [None]
