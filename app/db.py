"""SQLite: одно соединение на процесс, все обращения под общей блокировкой."""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    login TEXT NOT NULL UNIQUE,
    pass_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    team_id INTEGER,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL,
    addr_key TEXT NOT NULL,
    label TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    source TEXT NOT NULL,
    counted INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (team_id, addr_key, entry_id)
);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL,
    addr_key TEXT NOT NULL,
    label TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    source TEXT NOT NULL,
    due_at REAL NOT NULL,
    UNIQUE (team_id, entry_id)
);
CREATE TABLE IF NOT EXISTS answers (
    team_id INTEGER NOT NULL,
    q INTEGER NOT NULL,
    text TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (team_id, q)
);
CREATE TABLE IF NOT EXISTS submissions (
    team_id INTEGER PRIMARY KEY,
    submitted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    team_id INTEGER NOT NULL,
    q INTEGER NOT NULL,
    points REAL NOT NULL,
    PRIMARY KEY (team_id, q)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_team ON events (team_id, id);
"""

TEAM_TABLES = ("events", "pending", "answers", "submissions", "scores", "sessions")


class Database:
    def __init__(self, path: Path | str):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """Транзакция под блокировкой: всё внутри атомарно."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid
