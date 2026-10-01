"""SQLite 存储层：连接、建表与事务工具。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tide_planner.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS segments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node        INTEGER NOT NULL REFERENCES nodes (id),
    to_node          INTEGER NOT NULL REFERENCES nodes (id),
    travel_minutes   INTEGER NOT NULL CHECK (travel_minutes > 0),
    still_depth      REAL NOT NULL,
    bridge_clearance REAL
);

CREATE TABLE IF NOT EXISTS tide_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS tide_levels (
    revision_id INTEGER NOT NULL REFERENCES tide_revisions (id),
    minute      INTEGER NOT NULL CHECK (minute >= 0),
    level       REAL NOT NULL,
    PRIMARY KEY (revision_id, minute)
);

CREATE TABLE IF NOT EXISTS confirmations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id  INTEGER NOT NULL REFERENCES tide_revisions (id),
    request_json TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def db_path() -> str:
    """每次调用动态读取，便于测试与容器注入 DB_PATH。"""
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """建表（幂等），应用启动时调用。"""
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def immediate_transaction():
    """BEGIN IMMEDIATE 事务：确认行程等需要“读-复核-写”一致性的场景使用。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
