"""SQLite access layer.

Design notes
------------
* One short-lived connection per unit of work.  Every request (read or write)
  grabs its own connection, so no lock is held across segment assembly or
  across an HTTP response.
* Writers run inside ``BEGIN IMMEDIATE`` and therefore serialise on SQLite's
  single writer slot.  Because ``busy_timeout`` is set and the transaction is
  short, a second API process simply waits instead of failing.
* Readers run inside a deferred transaction and read one consistent snapshot
  of the committed state for the whole request.  In WAL mode readers never
  block the writer and the writer never blocks readers.
* Geometry is recomputed and persisted inside the same transaction that
  mutates points or roads.  Nothing is deferred, so a commit can never leave
  stale or partially updated geometry behind.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id          TEXT PRIMARY KEY,
    lon         REAL NOT NULL,
    lat         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS roads (
    id                TEXT PRIMARY KEY,
    geometry          TEXT,             -- materialised GeoJSON LineString, NULL = incomplete
    missing_point_ids TEXT NOT NULL DEFAULT '[]',
    revision          INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL,
    -- identity of the *current incarnation* of this road id; a delete followed
    -- by a recreate-with-same-id gets a fresh lineage, so old verdicts can
    -- never be inherited
    lineage           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS road_points (
    road_id     TEXT NOT NULL REFERENCES roads(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    -- deliberately NOT a foreign key to points: a dangling reference is a
    -- representable state (the road is incomplete and reports the ids) so
    -- that re-supplying the point restores the road automatically
    point_id    TEXT NOT NULL,
    PRIMARY KEY (road_id, seq),
    UNIQUE (road_id, point_id)
);
CREATE INDEX IF NOT EXISTS idx_road_points_point ON road_points(point_id);

CREATE TABLE IF NOT EXISTS store_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    generation  INTEGER NOT NULL
);
INSERT OR IGNORE INTO store_meta(id, generation) VALUES (1, 0);

-- QC verdicts on observed intersections.  A verdict binds the exact two road
-- incarnations (lineage + revision) that were seen at check time; any change
-- to either road's coordinates or references makes it invalid.  Rows are kept
-- after invalidation (they never silently mutate), and re-confirming the same
-- intersection writes a fresh verdict.  No foreign keys: a deleted road must
-- leave its history behind and simply count as "the road changed".
CREATE TABLE IF NOT EXISTS topology_verdicts (
    id                  TEXT PRIMARY KEY,
    road_a_id           TEXT NOT NULL,
    road_a_segment      INTEGER NOT NULL,
    road_b_id           TEXT NOT NULL,
    road_b_segment      INTEGER NOT NULL,
    road_a_lineage      TEXT NOT NULL,
    road_b_lineage      TEXT NOT NULL,
    road_a_revision     INTEGER NOT NULL,
    road_b_revision     INTEGER NOT NULL,
    intersection_point  TEXT NOT NULL,       -- [lon, lat] seen when confirmed
    status              TEXT NOT NULL,       -- confirmed | pending_review
    reason              TEXT NOT NULL DEFAULT '',
    generation          INTEGER NOT NULL,    -- committed state the verdict binds
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (road_a_id, road_a_segment, road_b_id, road_b_segment)
);
CREATE INDEX IF NOT EXISTS idx_verdicts_pair
    ON topology_verdicts(road_a_id, road_b_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before the topology feature existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(roads)")}
    if "lineage" not in cols:
        conn.execute("ALTER TABLE roads ADD COLUMN lineage TEXT")
        rows = conn.execute("SELECT id FROM roads").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE roads SET lineage = ? WHERE id = ?",
                (secrets.token_hex(8), row["id"]),
            )
    conn.execute(
        "UPDATE roads SET lineage = ? WHERE lineage IS NULL OR lineage = ''",
        (secrets.token_hex(8),),
    )


def new_lineage() -> str:
    """Incarnation id: regenerated on every road (re)creation."""
    return secrets.token_hex(8)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=settings.busy_timeout_s,
        isolation_level=None,          # transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % int(settings.busy_timeout_s * 1000))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db() -> None:
    """Create the schema; safe when both API processes start at once."""
    last: Exception | None = None
    for _ in range(10):
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            # executescript implicitly commits first; keep DDL idempotent and
            # do the guarded migration in its own serialised transaction.
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            try:
                _migrate(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return
        except sqlite3.OperationalError as exc:  # pragma: no cover - startup race
            last = exc
            time.sleep(0.1)
        finally:
            conn.close()
    raise last  # pragma: no cover


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """Deferred transaction -> one consistent committed snapshot per request."""
    conn = _connect()
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:  # pragma: no cover - read-only, nothing to undo
            pass
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Immediate transaction -> single writer, short critical section."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
