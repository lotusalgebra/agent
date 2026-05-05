"""Lightweight history DB for LOTUS pipeline runs.

Stores every pipeline run and every saved frame in a SQLite file under
`~/.lotus_auth/history.db`. Used by the Gallery tab to show what was
generated today/this week, grouped by pipeline and post.

API:
  start_run(name, source_md, parent_folder) -> run_id
  add_frame(run_id, post, frame_num, path, size, status="pending_review")
  update_frame_status(run_id, post, frame_num, status)
  end_run(run_id, status="completed")
  list_runs(limit=50) -> list[dict]
  frames_for_run(run_id) -> list[dict]

Schema is created on first use; safe to call from multiple threads.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

_DB_PATH = Path(os.path.expanduser("~/.lotus_auth/history.db"))
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(str(_DB_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init():
    with _lock, _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                source_md     TEXT,
                parent_folder TEXT,
                started_at    TEXT NOT NULL,
                ended_at      TEXT,
                status        TEXT NOT NULL DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS pipeline_frames (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      INTEGER NOT NULL,
                post        TEXT NOT NULL,
                frame_num   INTEGER NOT NULL,
                path        TEXT NOT NULL,
                size        INTEGER,
                status      TEXT NOT NULL DEFAULT 'pending_review',
                created_at  TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(id),
                UNIQUE(run_id, post, frame_num)
            );
            CREATE INDEX IF NOT EXISTS idx_frames_run ON pipeline_frames(run_id);
            -- Key-value settings so the Settings panel has a single
            -- backing store for user prefs (theme, voice profile,
            -- projects_root override, enabled tabs, etc.).
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with _lock, _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now()),
        )


def all_settings() -> dict:
    with _lock, _conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def delete_setting(key: str) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))


_init()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def start_run(name: str, source_md: Optional[str] = None,
              parent_folder: Optional[str] = None) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO pipeline_runs (name, source_md, parent_folder, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (name, source_md or "", parent_folder or "", _now()),
        )
        return int(cur.lastrowid)


def end_run(run_id: int, status: str = "completed") -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE pipeline_runs SET ended_at=?, status=? WHERE id=?",
            (_now(), status, run_id),
        )


def add_frame(run_id: int, post: str, frame_num: int, path: str,
              size: Optional[int] = None,
              status: str = "pending_review") -> None:
    if size is None:
        try: size = os.path.getsize(path)
        except Exception: size = 0
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO pipeline_frames "
            "(run_id, post, frame_num, path, size, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, post, frame_num, path, size, status, _now()),
        )


def update_frame_status(run_id: int, post: str, frame_num: int,
                        status: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE pipeline_frames SET status=? "
            "WHERE run_id=? AND post=? AND frame_num=?",
            (status, run_id, post, frame_num),
        )


def list_runs(limit: int = 50) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT r.*, "
            "  (SELECT COUNT(*) FROM pipeline_frames f WHERE f.run_id=r.id) AS frame_count, "
            "  (SELECT COUNT(*) FROM pipeline_frames f WHERE f.run_id=r.id AND f.status='approved') AS approved_count "
            "FROM pipeline_runs r ORDER BY r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def frames_for_run(run_id: int) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM pipeline_frames WHERE run_id=? "
            "ORDER BY post, frame_num",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _lock, _conn() as c:
        runs = c.execute("SELECT COUNT(*) AS n FROM pipeline_runs").fetchone()["n"]
        frames = c.execute("SELECT COUNT(*) AS n FROM pipeline_frames").fetchone()["n"]
        approved = c.execute(
            "SELECT COUNT(*) AS n FROM pipeline_frames WHERE status='approved'"
        ).fetchone()["n"]
    return {"runs": runs, "frames": frames, "approved": approved,
            "db_path": str(_DB_PATH)}
