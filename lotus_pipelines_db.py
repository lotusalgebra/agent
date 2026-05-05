"""SQLite persistence for the multi-pipeline registry.

Mother keeps `_pipelines` (in-memory dict) for the live registry. This module
mirrors that to disk so:
  - the A2 "All Pipelines" overview can show *every* pipeline ever started,
    including completed/cancelled ones whose in-memory entries were dropped;
  - non-terminal pipelines survive an agent restart (rehydration policy 3c).

DB lives at `~/.lotus_auth/pipelines.db` — separate from prompts.db and
history.db on purpose. Wiping pipeline metadata must not touch the prompt
review gate or the run history.

Public API (mirrors `lotus_prompts_db` shape):
  upsert(p_dict) -> None
  mark_cancelled(pipeline_id) -> None
  set_stage(pipeline_id, stage) -> None
  list_all() -> list[dict]
  list_active() -> list[dict]              # non-terminal, for boot rehydrate
  get(pipeline_id) -> Optional[dict]
  delete(pipeline_id) -> None
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

_DB_PATH = Path(os.path.expanduser("~/.lotus_auth/pipelines.db"))
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()

# Stages that mean "this pipeline is finished — don't rehydrate on boot."
TERMINAL_STAGES = ("done",)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipelines (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                source_md    TEXT,
                source_hash  TEXT,
                stage        TEXT NOT NULL,
                paused       INTEGER NOT NULL DEFAULT 0,
                cancelled    INTEGER NOT NULL DEFAULT 0,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL,
                folder_name  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pipelines_stage
                ON pipelines(stage, cancelled);
            CREATE INDEX IF NOT EXISTS idx_pipelines_created
                ON pipelines(created_at DESC);
            """
        )
        # Backfill the column on DBs that pre-date A10. Failure to add (e.g.
        # column already exists from a fresh CREATE TABLE above) is safe to
        # ignore.
        try:
            c.execute("ALTER TABLE pipelines ADD COLUMN folder_name TEXT")
        except Exception:
            pass


_init()


def _row_to_dict(row: sqlite3.Row) -> dict:
    # `folder_name` is on rows created by A10+. Older rows return None
    # (rehydrated pipelines fall back to the brand+date folder).
    try:
        folder_name = row["folder_name"]
    except Exception:
        folder_name = None
    return {
        "id":          row["id"],
        "name":        row["name"],
        "source_md":   row["source_md"],
        "source_hash": row["source_hash"],
        "stage":       row["stage"],
        "paused":      bool(row["paused"]),
        "cancelled":   bool(row["cancelled"]),
        "created_at":  row["created_at"],
        "updated_at":  row["updated_at"],
        "folder_name": folder_name,
    }


def upsert(p: dict) -> None:
    """Insert or replace a pipeline row. Accepts a Pipeline.summary()-shaped
    dict OR a Pipeline.as_event()-shaped dict — only the metadata fields are
    read; frame/prompt arrays are ignored (they live in their own DBs).

    Called from `_pipeline_broadcast` so every state transition gets snapshot.
    """
    pid = p.get("id")
    if not pid:
        return
    now = datetime.utcnow().timestamp()
    row = (
        pid,
        p.get("name") or f"Pipeline {pid}",
        p.get("source_md"),
        p.get("source_hash"),
        p.get("stage") or "ask_location",
        1 if p.get("paused") else 0,
        1 if p.get("cancelled") else 0,
        float(p.get("created_at") or now),
        now,
        p.get("folder_name"),
    )
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO pipelines
              (id, name, source_md, source_hash, stage, paused, cancelled,
               created_at, updated_at, folder_name)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name        = excluded.name,
              source_md   = excluded.source_md,
              source_hash = excluded.source_hash,
              stage       = excluded.stage,
              paused      = excluded.paused,
              cancelled   = excluded.cancelled,
              updated_at  = excluded.updated_at,
              folder_name = COALESCE(excluded.folder_name, pipelines.folder_name)
            """,
            row,
        )


def mark_cancelled(pipeline_id: str) -> None:
    """Persist that a pipeline was cancelled. Row is kept (history) — only
    `delete()` removes it."""
    if not pipeline_id:
        return
    now = datetime.utcnow().timestamp()
    with _lock, _conn() as c:
        c.execute(
            "UPDATE pipelines SET cancelled = 1, updated_at = ? WHERE id = ?",
            (now, pipeline_id),
        )


def set_stage(pipeline_id: str, stage: str) -> None:
    if not pipeline_id or not stage:
        return
    now = datetime.utcnow().timestamp()
    with _lock, _conn() as c:
        c.execute(
            "UPDATE pipelines SET stage = ?, updated_at = ? WHERE id = ?",
            (stage, now, pipeline_id),
        )


def list_all() -> list[dict]:
    """Every pipeline row, newest first. Used by the A2 overview screen."""
    with _lock, _conn() as c:
        cur = c.execute("SELECT * FROM pipelines ORDER BY created_at DESC")
        return [_row_to_dict(r) for r in cur.fetchall()]


def list_active() -> list[dict]:
    """Non-terminal, non-cancelled pipelines. Used by `LotusPhase1.__init__`
    to rehydrate `_pipelines` on agent boot (rehydration policy 3c — terminal
    stages don't come back as live chips, only as overview rows)."""
    placeholders = ",".join("?" * len(TERMINAL_STAGES))
    with _lock, _conn() as c:
        cur = c.execute(
            f"""
            SELECT * FROM pipelines
            WHERE cancelled = 0 AND stage NOT IN ({placeholders})
            ORDER BY created_at ASC
            """,
            TERMINAL_STAGES,
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def get(pipeline_id: str) -> Optional[dict]:
    if not pipeline_id:
        return None
    with _lock, _conn() as c:
        cur = c.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def delete(pipeline_id: str) -> None:
    if not pipeline_id:
        return
    with _lock, _conn() as c:
        c.execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,))
