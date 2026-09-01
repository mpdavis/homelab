"""Shared fixtures.

Every test that touches the database gets its own DuckDB file in a temp
directory. The settings cache and the module-level connection both have to be
cleared around that, or a test would quietly reuse whichever database ran
first — which is exactly the kind of bug that makes a suite pass locally and
fail in CI.
"""

from __future__ import annotations

import pytest

import synth

from gridiron import db as db_module
from gridiron.config import settings


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the whole package at a throwaway database."""
    monkeypatch.setenv("GRIDIRON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GRIDIRON_DB_FILENAME", "test.duckdb")
    settings.cache_clear()
    db_module.reset_connection()
    yield tmp_path
    db_module.reset_connection()
    settings.cache_clear()


@pytest.fixture
def conn(data_dir):
    """An empty, migrated database."""
    return db_module.connection()


@pytest.fixture
def populated(conn):
    """The synthetic universe, ingested but with no features built yet."""
    synth.build(conn)
    return conn


@pytest.fixture
def featured(populated):
    """The synthetic universe with ``team_game`` built."""
    from gridiron.features.build import build_team_game

    build_team_game(populated, list(synth.SEASONS))
    return populated


@pytest.fixture
def wide_history(conn):
    """A 28-team, six-season league with no play-by-play.

    ``refit_curve=False`` because there are no drives to fit one from — the
    market-facing analyses only read the scoreboard and the line.
    """
    from gridiron.features.build import build_team_game

    synth.build_wide(conn)
    build_team_game(conn, list(synth.LONG_SEASONS), refit_curve=False)
    return conn


@pytest.fixture
def long_history(conn):
    """Six seasons, built.

    The analysis module declines to report on thin samples, so testing those
    paths needs enough history to clear its floors — which is the point of
    having the floors, and the reason this fixture exists separately rather
    than the floors being lowered for tests.
    """
    from gridiron.features.build import build_team_game

    synth.build(conn, seasons=synth.LONG_SEASONS)
    build_team_game(conn, list(synth.LONG_SEASONS))
    return conn
