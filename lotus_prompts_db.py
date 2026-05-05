"""SQLite store for extracted prompts under review.

Persists every prompt the parser pulls out of a source markdown file so the
review gate survives mother restarts. Each row carries the prompt's slide
title, parsed topic/slide numbers, original text, optional user edit, and a
status (`pending` / `approved` / `rejected`). The pipeline only burst-sends
rows that are `approved`.

DB lives at `~/.lotus_auth/prompts.db` — separate from the run-history DB
so wiping prompt review state does not touch generation history.

Public API:
  add_batch(source_path, source_hash, candidates) -> list[dict]
  list_for_source(source_hash) -> list[dict]
  set_status(prompt_id, status) -> None
  update_text(prompt_id, edited_text) -> None
  count_by_status(source_hash) -> dict[str, int]
  approved_text_for(source_hash) -> list[dict]   # used by generation step
  set_status_all(source_hash, status) -> None
  reset_for_source(source_hash) -> None
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

_DB_PATH = Path(os.path.expanduser("~/.lotus_auth/prompts.db"))
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _init() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path   TEXT NOT NULL,
                source_hash   TEXT NOT NULL,
                slide_title   TEXT,
                topic_number  INTEGER,
                slide_number  INTEGER,
                source_type   TEXT,
                text          TEXT NOT NULL,
                text_hash     TEXT NOT NULL,
                edited_text   TEXT,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                UNIQUE(source_hash, text_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_prompts_source
                ON prompts(source_hash, slide_number);
            CREATE INDEX IF NOT EXISTS idx_prompts_status
                ON prompts(source_hash, status);
            """
        )


_init()


# ── helpers ────────────────────────────────────────────────────────────────

_TITLE_TOPIC_RE = re.compile(r"\bTOPIC\s*(\d+)\b", re.IGNORECASE)
_TITLE_SLIDE_RE = re.compile(r"\bSLIDE\s*(\d+)\b", re.IGNORECASE)


def file_hash(path: str) -> str:
    """SHA1 of the file contents — used as source_hash so re-uploads of the
    same file collide on (source_hash, text_hash) and don't double-insert."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return hashlib.sha1(norm.encode("utf-8", errors="replace")).hexdigest()


def _parse_numbers(title: str) -> tuple[Optional[int], Optional[int]]:
    if not title:
        return None, None
    tm = _TITLE_TOPIC_RE.search(title)
    sm = _TITLE_SLIDE_RE.search(title)
    return (int(tm.group(1)) if tm else None,
            int(sm.group(1)) if sm else None)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id":            row["id"],
        "source_path":   row["source_path"],
        "source_hash":   row["source_hash"],
        "slide_title":   row["slide_title"],
        "topic_number":  row["topic_number"],
        "slide_number":  row["slide_number"],
        "source_type":   row["source_type"],
        "text":          row["text"],
        "edited_text":   row["edited_text"],
        "status":        row["status"],
        "created_at":    row["created_at"],
        "updated_at":    row["updated_at"],
    }


# ── public API ────────────────────────────────────────────────────────────

def add_batch(source_path: str, source_hash: str,
              candidates: Iterable[dict]) -> list[dict]:
    """Insert a batch of prompts (idempotent — UNIQUE on source_hash+text_hash).

    Each candidate dict must have at least 'text'; 'title' and 'source' are
    optional. Returns the rows for this source after insert (whether new or
    pre-existing), ordered by topic/slide if known, else by id.
    """
    now = datetime.utcnow().isoformat()
    rows = []
    for cand in candidates:
        text = (cand.get("text") or "").strip()
        if not text:
            continue
        title = (cand.get("title") or "").strip()
        topic_n, slide_n = _parse_numbers(title)
        rows.append((
            source_path, source_hash,
            title, topic_n, slide_n,
            cand.get("source") or "",
            text, text_hash(text),
            "pending",
            now, now,
        ))
    if rows:
        with _lock, _conn() as c:
            c.executemany(
                """
                INSERT OR IGNORE INTO prompts
                (source_path, source_hash, slide_title, topic_number,
                 slide_number, source_type, text, text_hash, status,
                 created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
    return list_for_source(source_hash)


def list_for_source(source_hash: str) -> list[dict]:
    with _lock, _conn() as c:
        cur = c.execute(
            """
            SELECT * FROM prompts
            WHERE source_hash = ?
            ORDER BY
                CASE WHEN topic_number IS NULL THEN 1 ELSE 0 END,
                topic_number,
                CASE WHEN slide_number IS NULL THEN 1 ELSE 0 END,
                slide_number,
                id
            """,
            (source_hash,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def set_status(prompt_id: int, status: str) -> None:
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status: {status!r}")
    with _lock, _conn() as c:
        c.execute(
            "UPDATE prompts SET status=?, updated_at=? WHERE id=?",
            (status, datetime.utcnow().isoformat(), prompt_id),
        )


def update_text(prompt_id: int, edited_text: str) -> None:
    """Store user-edited text without losing the original. The pipeline reads
    edited_text first, falling back to text. Bumps status back to 'pending'
    so the user re-approves after editing."""
    with _lock, _conn() as c:
        c.execute(
            """
            UPDATE prompts
               SET edited_text=?, status='pending', updated_at=?
             WHERE id=?
            """,
            (edited_text, datetime.utcnow().isoformat(), prompt_id),
        )


def set_status_all(source_hash: str, status: str) -> int:
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status: {status!r}")
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE prompts SET status=?, updated_at=? WHERE source_hash=?",
            (status, datetime.utcnow().isoformat(), source_hash),
        )
        return cur.rowcount


def count_by_status(source_hash: str) -> dict[str, int]:
    with _lock, _conn() as c:
        cur = c.execute(
            """
            SELECT status, COUNT(*) AS n FROM prompts
            WHERE source_hash=? GROUP BY status
            """,
            (source_hash,),
        )
        out = {"pending": 0, "approved": 0, "rejected": 0}
        for r in cur.fetchall():
            out[r["status"]] = r["n"]
        return out


def approved_text_for(source_hash: str) -> list[dict]:
    """Return the list the generation step should burst-send. Each item:
       {id, slide_title, topic_number, slide_number, text}
    where 'text' is edited_text if the user edited the prompt, else the
    original parsed text. Ordered by topic/slide for stable post grouping.
    """
    rows = []
    with _lock, _conn() as c:
        cur = c.execute(
            """
            SELECT id, slide_title, topic_number, slide_number,
                   text, edited_text
              FROM prompts
             WHERE source_hash=? AND status='approved'
             ORDER BY
                CASE WHEN topic_number IS NULL THEN 1 ELSE 0 END,
                topic_number,
                CASE WHEN slide_number IS NULL THEN 1 ELSE 0 END,
                slide_number,
                id
            """,
            (source_hash,),
        )
        for r in cur.fetchall():
            rows.append({
                "id":           r["id"],
                "slide_title":  r["slide_title"],
                "topic_number": r["topic_number"],
                "slide_number": r["slide_number"],
                "text":         r["edited_text"] or r["text"],
            })
    return rows


def reset_for_source(source_hash: str) -> int:
    """Delete all rows for this source. Used when the user wants a clean
    re-extract of the same file."""
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM prompts WHERE source_hash=?", (source_hash,),
        )
        return cur.rowcount


def get_by_id(prompt_id: int) -> Optional[dict]:
    with _lock, _conn() as c:
        cur = c.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,))
        r = cur.fetchone()
        return _row_to_dict(r) if r else None


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "stats":
        if len(sys.argv) >= 3:
            sh = sys.argv[2]
            print(count_by_status(sh))
            for row in list_for_source(sh):
                print(f"  [{row['status']}] {row['slide_title'][:50]} "
                      f"({row['topic_number']}/{row['slide_number']})")
        else:
            print(f"DB at {_DB_PATH}")
            with _lock, _conn() as c:
                cur = c.execute(
                    "SELECT source_hash, COUNT(*) FROM prompts "
                    "GROUP BY source_hash"
                )
                for row in cur.fetchall():
                    print(f"  {row[0][:12]}…  {row[1]} prompts")
