"""SQLite store for extracted prompts under review.

Persists every prompt the parser pulls out of a source markdown file so the
review gate survives mother restarts. Each row carries the prompt's slide
title, parsed topic/slide numbers, original text, optional user edit, and a
status (`pending` / `approved` / `rejected`). The pipeline only burst-sends
rows that are `approved`.

DB lives at `~/.lotus_auth/prompts.db` — separate from the run-history DB
so wiping prompt review state does not touch generation history. Override the
path with `LOTUS_PROMPTS_DB` (used by the migration test harness so it can run
against a copy without touching live review state).

Ownership model (one-to-many):
  Prompts belong to a PIPELINE, not to a source file. Each row carries a
  `pipeline_id` (== `pipelines.id` in pipelines.db) and uniqueness is
  `UNIQUE(pipeline_id, text_hash)`. Two pipelines built from the same source
  .md therefore get INDEPENDENT prompt rows and review state — approving in
  one no longer bleeds into the other. `source_hash` is retained as a
  provenance column (which file the prompt came from) but is no longer the
  identity key. The pipeline_id link is a *logical* FK: pipelines live in a
  separate DB file, so SQLite can't enforce it.

Public API:
  add_batch(source_path, source_hash, pipeline_id, candidates) -> list[dict]
  list_for_pipeline(pipeline_id) -> list[dict]
  set_status(prompt_id, status) -> None
  update_text(prompt_id, edited_text) -> None
  count_by_status(pipeline_id) -> dict[str, int]
  approved_text_for(pipeline_id) -> list[dict]   # used by generation step
  set_status_all(pipeline_id, status) -> int
  approve_pending(pipeline_id) -> int
  reset_for_pipeline(pipeline_id) -> int
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

_DB_PATH = Path(os.path.expanduser(
    os.environ.get("LOTUS_PROMPTS_DB", "~/.lotus_auth/prompts.db")))
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Read-only source for the migration's source_hash -> pipeline_id map. Kept as
# a plain path (not an import of lotus_pipelines_db) so migration has no module
# coupling and the test harness can point it at a copy.
_PIPELINES_DB_PATH = Path(os.path.expanduser(
    os.environ.get("LOTUS_PIPELINES_DB", "~/.lotus_auth/pipelines.db")))

_lock = threading.Lock()

_NEW_SCHEMA = """
    CREATE TABLE IF NOT EXISTS prompts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_id   TEXT NOT NULL DEFAULT '',
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
        UNIQUE(pipeline_id, text_hash)
    );

    CREATE INDEX IF NOT EXISTS idx_prompts_pipeline
        ON prompts(pipeline_id, slide_number);
    CREATE INDEX IF NOT EXISTS idx_prompts_pipeline_status
        ON prompts(pipeline_id, status);
    CREATE INDEX IF NOT EXISTS idx_prompts_source
        ON prompts(source_hash);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _table_columns(c: sqlite3.Connection, table: str) -> list[str]:
    try:
        cur = c.execute(f"PRAGMA table_info({table})")
        return [r[1] for r in cur.fetchall()]
    except Exception:
        return []


def _source_hash_to_pipeline_ids() -> dict[str, list[str]]:
    """Map every source_hash to the pipeline ids that used it, read from
    pipelines.db. Used only by the one-time migration to clone legacy
    file-keyed rows into each owning pipeline."""
    out: dict[str, list[str]] = {}
    if not _PIPELINES_DB_PATH.exists():
        return out
    try:
        pc = sqlite3.connect(str(_PIPELINES_DB_PATH), timeout=10)
        try:
            cur = pc.execute(
                "SELECT id, source_hash FROM pipelines "
                "WHERE source_hash IS NOT NULL AND source_hash != ''"
            )
            for pid, sh in cur.fetchall():
                if pid:
                    out.setdefault(sh, []).append(pid)
        finally:
            pc.close()
    except Exception as e:
        print(f"[prompts-db] migration: could not read pipelines.db ({e})")
    return out


def _migrate_to_pipeline_id(c: sqlite3.Connection) -> None:
    """One-time rebuild: file-keyed schema -> pipeline-keyed schema.

    Old rows keyed by (source_hash, text_hash) are CLONED into every pipeline
    that used their source file, preserving each row's status/edited_text so no
    pipeline loses its review state. Rows whose source_hash matches no known
    pipeline are preserved under a synthetic pipeline_id == source_hash (keeps
    them addressable and prevents UNIQUE collapse across different files).

    The pre-migration table is kept as `prompts_pre_pipeline_id_backup` (in the
    same DB) as a safety net in addition to the manual file snapshot.
    """
    cols = _table_columns(c, "prompts")
    if not cols:
        return  # no legacy table — fresh DB, _create handles it
    if "pipeline_id" in cols:
        return  # already migrated

    print("[prompts-db] migrating prompts.db -> pipeline-keyed schema…")
    sh_map = _source_hash_to_pipeline_ids()

    # Snapshot the old table, then build the new one alongside it.
    c.execute("DROP TABLE IF EXISTS prompts_pre_pipeline_id_backup")
    c.execute("ALTER TABLE prompts RENAME TO prompts_pre_pipeline_id_backup")
    c.executescript(_NEW_SCHEMA)

    old = c.execute("SELECT * FROM prompts_pre_pipeline_id_backup").fetchall()
    cloned = 0
    for r in old:
        sh = r["source_hash"]
        targets = sh_map.get(sh) or [sh]  # orphan -> synthetic pid = source_hash
        for pid in targets:
            c.execute(
                """
                INSERT OR IGNORE INTO prompts
                    (pipeline_id, source_path, source_hash, slide_title,
                     topic_number, slide_number, source_type, text, text_hash,
                     edited_text, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (pid, r["source_path"], r["source_hash"], r["slide_title"],
                 r["topic_number"], r["slide_number"], r["source_type"],
                 r["text"], r["text_hash"], r["edited_text"], r["status"],
                 r["created_at"], r["updated_at"]),
            )
            cloned += 1
    print(f"[prompts-db] migration done: {len(old)} legacy row(s) -> "
          f"{cloned} pipeline-scoped row(s). Backup kept in "
          f"prompts_pre_pipeline_id_backup.")


def _init() -> None:
    with _lock, _conn() as c:
        _migrate_to_pipeline_id(c)
        c.executescript(_NEW_SCHEMA)


_init()


# ── helpers ────────────────────────────────────────────────────────────────

_TITLE_TOPIC_RE = re.compile(r"\bTOPIC\s*(\d+)\b", re.IGNORECASE)
_TITLE_SLIDE_RE = re.compile(r"\bSLIDE\s*(\d+)\b", re.IGNORECASE)


def file_hash(path: str) -> str:
    """SHA1 of the file contents — recorded as `source_hash` provenance and
    used for within-pipeline dedup context. No longer the identity key."""
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
        "pipeline_id":   row["pipeline_id"],
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

def add_batch(source_path: str, source_hash: str, pipeline_id: str,
              candidates: Iterable[dict]) -> list[dict]:
    """Insert a batch of prompts for one pipeline (idempotent — UNIQUE on
    pipeline_id+text_hash, so re-uploading the same file into the SAME pipeline
    dedupes and preserves prior approve/reject/edit state).

    Each candidate dict must have at least 'text'; 'title' and 'source' are
    optional. Returns the rows for this pipeline after insert (whether new or
    pre-existing), ordered by topic/slide if known, else by id.
    """
    if not pipeline_id:
        raise ValueError("add_batch requires a pipeline_id")
    now = datetime.utcnow().isoformat()
    rows = []
    for cand in candidates:
        text = (cand.get("text") or "").strip()
        if not text:
            continue
        title = (cand.get("title") or "").strip()
        topic_n, slide_n = _parse_numbers(title)
        rows.append((
            pipeline_id, source_path, source_hash,
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
                (pipeline_id, source_path, source_hash, slide_title,
                 topic_number, slide_number, source_type, text, text_hash,
                 status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
    return list_for_pipeline(pipeline_id)


_ORDER_BY = """
    ORDER BY
        CASE WHEN topic_number IS NULL THEN 1 ELSE 0 END,
        topic_number,
        CASE WHEN slide_number IS NULL THEN 1 ELSE 0 END,
        slide_number,
        id
"""


def list_for_pipeline(pipeline_id: str) -> list[dict]:
    with _lock, _conn() as c:
        cur = c.execute(
            f"SELECT * FROM prompts WHERE pipeline_id = ? {_ORDER_BY}",
            (pipeline_id,),
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


def set_status_all(pipeline_id: str, status: str) -> int:
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status: {status!r}")
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE prompts SET status=?, updated_at=? WHERE pipeline_id=?",
            (status, datetime.utcnow().isoformat(), pipeline_id),
        )
        return cur.rowcount


def approve_pending(pipeline_id: str) -> int:
    """Flip only the still-pending rows to approved (a previously-rejected
    prompt is never silently re-approved). Returns rows affected."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE prompts SET status='approved', updated_at=? "
            "WHERE pipeline_id=? AND status='pending'",
            (datetime.utcnow().isoformat(), pipeline_id),
        )
        return cur.rowcount


def count_by_status(pipeline_id: str) -> dict[str, int]:
    with _lock, _conn() as c:
        cur = c.execute(
            "SELECT status, COUNT(*) AS n FROM prompts "
            "WHERE pipeline_id=? GROUP BY status",
            (pipeline_id,),
        )
        out = {"pending": 0, "approved": 0, "rejected": 0}
        for r in cur.fetchall():
            out[r["status"]] = r["n"]
        return out


def approved_text_for(pipeline_id: str) -> list[dict]:
    """Return the list the generation step should burst-send. Each item:
       {id, slide_title, topic_number, slide_number, text}
    where 'text' is edited_text if the user edited the prompt, else the
    original parsed text. Ordered by topic/slide for stable post grouping.
    """
    rows = []
    with _lock, _conn() as c:
        cur = c.execute(
            f"""
            SELECT id, slide_title, topic_number, slide_number,
                   text, edited_text
              FROM prompts
             WHERE pipeline_id=? AND status='approved'
             {_ORDER_BY}
            """,
            (pipeline_id,),
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


def reset_for_pipeline(pipeline_id: str) -> int:
    """Delete all rows for this pipeline. Used when the user wants a clean
    re-extract into the same pipeline."""
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM prompts WHERE pipeline_id=?", (pipeline_id,),
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
            pid = sys.argv[2]
            print(count_by_status(pid))
            for row in list_for_pipeline(pid):
                print(f"  [{row['status']}] {row['slide_title'][:50]} "
                      f"({row['topic_number']}/{row['slide_number']})")
        else:
            print(f"DB at {_DB_PATH}")
            with _lock, _conn() as c:
                cur = c.execute(
                    "SELECT pipeline_id, COUNT(*) FROM prompts "
                    "GROUP BY pipeline_id"
                )
                for row in cur.fetchall():
                    print(f"  {str(row[0])[:16]}…  {row[1]} prompts")
