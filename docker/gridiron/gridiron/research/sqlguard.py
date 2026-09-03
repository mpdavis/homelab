"""Validation for model-written SQL before it touches the database.

A proposer that can write SQL can write `DROP TABLE`, and a harness that runs
generated SQL unchecked against a live database is one bad completion away from
losing the dataset. So nothing reaches `execute` without passing through here.

The check is a parse, not a keyword scan. DuckDB will tell us the statement
type of each statement it finds, which settles questions a regular expression
gets wrong in both directions — a comment containing the word DELETE is fine, a
CTE that wraps a COPY is not.
"""

from __future__ import annotations

import re

import duckdb

# Functions that reach outside the database. All of them are legal inside a
# SELECT, none of them have any business in a feature block, and one of them in
# generated SQL would turn "read some football data" into "read the filesystem".
FILE_ACCESS = (
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "parquet_scan", "csv_scan", "delta_scan",
    "iceberg_scan", "postgres_scan", "sqlite_scan", "mysql_scan", "install",
    "load_extension",
)

# Columns a feature block must produce to be joinable onto team_game.
REQUIRED_COLUMNS = ("game_id", "team")

MAX_ROWS = 2_000_000


class UnsafeSQL(ValueError):
    """The generated SQL is not a plain read, or does not fit the contract."""


def validate(sql: str) -> str:
    """Return the SQL if it is a single read-only SELECT, else raise."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise UnsafeSQL("empty SQL")

    try:
        statements = duckdb.connect().extract_statements(text)
    except Exception as exc:  # noqa: BLE001 — a parse failure is a rejection
        raise UnsafeSQL(f"will not parse: {exc}") from exc

    if len(statements) != 1:
        raise UnsafeSQL(
            f"expected exactly one statement, found {len(statements)}. A feature "
            "block is a single SELECT."
        )

    kind = str(statements[0].type).rsplit(".", 1)[-1]
    if kind != "SELECT":
        raise UnsafeSQL(
            f"statement is {kind}, not SELECT. The proposer may only read: "
            "COPY, ATTACH, INSERT, UPDATE, DELETE and DDL are all refused."
        )

    lowered = text.lower()
    for name in FILE_ACCESS:
        if re.search(rf"\b{re.escape(name)}\s*\(", lowered):
            raise UnsafeSQL(
                f"calls {name}(), which reads outside the database. A feature "
                "block reads the ingested tables and nothing else."
            )
    return text


def run(conn, sql: str, *, seasons: list[int] | None = None):
    """Validate, execute, and check the result fits the feature contract.

    ``seasons`` is applied by the caller inside the SQL rather than here; this
    only enforces shape, so a block that ignores the season filter fails on the
    holdout guard rather than silently over-reaching.
    """
    text = validate(sql)
    frame = conn.execute(f"SELECT * FROM ({text}) AS _block LIMIT {MAX_ROWS}").df()

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise UnsafeSQL(
            f"result is missing {missing}. A feature block returns one row per "
            f"(game_id, team) plus its metric columns; got {list(frame.columns)}."
        )

    metrics = [
        c
        for c in frame.columns
        if c not in REQUIRED_COLUMNS and _is_numeric(frame[c])
    ]
    if not metrics:
        raise UnsafeSQL(
            "result has no numeric metric column beyond game_id and team, so "
            "there is nothing to test."
        )
    if frame.empty:
        raise UnsafeSQL("result is empty over the requested seasons")
    return frame, metrics


def _is_numeric(series) -> bool:
    import pandas as pd

    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(
        series
    )
