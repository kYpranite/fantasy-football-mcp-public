"""SQLite connection setup and schema migrations."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, Union

from src.storage.schema import MIGRATIONS, SCHEMA_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "league.db"


def default_db_path() -> Path:
    """``LEAGUE_DB_PATH`` if set, else ``data/league.db`` (gitignored)."""
    return Path(os.environ.get("LEAGUE_DB_PATH") or DEFAULT_DB_PATH)


def connect(path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Open (creating if needed) the league database and apply pending migrations.

    WAL journaling lets the MCP server read while a sync writes; readers keep seeing
    the previous committed data until the sync's transaction commits.
    """
    db_path = Path(path) if path is not None else default_db_path()
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # explicit BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply migrations newer than the database's ``user_version``; returns the version."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{current} is newer than this code (v{SCHEMA_VERSION}); update the code."
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_statements(MIGRATIONS[version - 1]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


def _split_statements(script: str):
    """Split a migration script into statements (``executescript`` would auto-commit)."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        if line.strip().startswith("--"):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer.strip()
            buffer = ""
    if buffer.strip():
        raise ValueError(f"Incomplete SQL statement in migration: {buffer[:80]!r}")
