"""SQLite store for the Upwork management agent.

Two tables:
  - `jobs`         — every job card scraped from the Best Matches feed,
                     scored, optionally with a drafted proposal.
  - `user_profile` — single-row config the agent reads when scoring/
                     drafting (skills, rate, niches, decline-list).

DB at `~/.lotus_auth/upwork.db` — separate from Lotus's other stores so
wiping it doesn't affect pipelines.

Public API (mirrors lotus_pipelines_db patterns):
  upsert_job(j) -> None
  list_jobs(*, status=None, min_score=None, limit=50) -> list[dict]
  get_job(job_id) -> Optional[dict]
  set_job_status(job_id, status) -> None
  set_job_score(job_id, score, reason) -> None
  set_job_proposal(job_id, text) -> None
  get_profile() -> dict
  set_profile(profile) -> None
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

_DB_PATH = Path(os.path.expanduser("~/.lotus_auth/upwork.db"))
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()

# Job status lifecycle.
STATUSES = ("new", "scored", "drafted", "submitted", "dismissed", "expired")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,         -- Upwork's job hash
                url           TEXT NOT NULL,
                title         TEXT NOT NULL,
                description   TEXT,
                budget        TEXT,                     -- "$50" or "$30-$80"
                budget_type   TEXT,                     -- "fixed" / "hourly"
                client_country TEXT,
                client_rating REAL,
                client_spent  TEXT,
                posted_at     TEXT,                     -- as Upwork shows it
                fetched_at    TEXT NOT NULL,
                score         REAL,
                score_reason  TEXT,
                proposal_text TEXT,
                proposal_drafted_at TEXT,
                status        TEXT NOT NULL DEFAULT 'new',
                raw_json      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status_score
                ON jobs(status, score DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_fetched
                ON jobs(fetched_at DESC);

            CREATE TABLE IF NOT EXISTS user_profile (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                profile_json TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            """
        )


_init()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ── Jobs ─────────────────────────────────────────────────────────────────

def upsert_job(j: dict) -> None:
    """Insert or update a job row. `id` is required. Other fields are
    optional. Existing scoring / proposal / status fields are preserved
    on upsert (we never silently overwrite a user-applied status)."""
    jid = j.get("id")
    if not jid:
        return
    now = datetime.utcnow().isoformat()
    fields = {
        "id":             jid,
        "url":            j.get("url") or "",
        "title":          j.get("title") or "",
        "description":    j.get("description"),
        "budget":         j.get("budget"),
        "budget_type":    j.get("budget_type"),
        "client_country": j.get("client_country"),
        "client_rating":  j.get("client_rating"),
        "client_spent":   j.get("client_spent"),
        "posted_at":      j.get("posted_at"),
        "fetched_at":     j.get("fetched_at") or now,
        "raw_json":       json.dumps(j.get("raw") or {}),
    }
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO jobs (id, url, title, description, budget,
                              budget_type, client_country, client_rating,
                              client_spent, posted_at, fetched_at, raw_json)
            VALUES (:id, :url, :title, :description, :budget, :budget_type,
                    :client_country, :client_rating, :client_spent,
                    :posted_at, :fetched_at, :raw_json)
            ON CONFLICT(id) DO UPDATE SET
                title          = COALESCE(NULLIF(excluded.title, ''),          jobs.title),
                description    = COALESCE(excluded.description,                 jobs.description),
                budget         = COALESCE(excluded.budget,                      jobs.budget),
                budget_type    = COALESCE(excluded.budget_type,                 jobs.budget_type),
                client_country = COALESCE(excluded.client_country,              jobs.client_country),
                client_rating  = COALESCE(excluded.client_rating,               jobs.client_rating),
                client_spent   = COALESCE(excluded.client_spent,                jobs.client_spent),
                posted_at      = COALESCE(excluded.posted_at,                   jobs.posted_at),
                fetched_at     = excluded.fetched_at,
                raw_json       = COALESCE(NULLIF(excluded.raw_json, '{}'),     jobs.raw_json)
            """,
            fields,
        )


def list_jobs(*, status: Optional[str] = None,
              min_score: Optional[float] = None,
              limit: int = 50) -> list[dict]:
    where, params = [], []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if min_score is not None:
        where.append("score >= ?")
        params.append(float(min_score))
    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(score, -1) DESC, fetched_at DESC LIMIT ?"
    params.append(int(limit))
    with _lock, _conn() as c:
        cur = c.execute(sql, params)
        return [_row_to_dict(r) for r in cur.fetchall()]


def get_job(job_id: str) -> Optional[dict]:
    with _lock, _conn() as c:
        cur = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def set_job_status(job_id: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    with _lock, _conn() as c:
        c.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def set_job_score(job_id: str, score: float, reason: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE jobs SET score = ?, score_reason = ?, "
            "status = CASE WHEN status = 'new' THEN 'scored' ELSE status END "
            "WHERE id = ?",
            (float(score), reason or "", job_id),
        )


def set_job_proposal(job_id: str, text: str) -> None:
    now = datetime.utcnow().isoformat()
    with _lock, _conn() as c:
        c.execute(
            "UPDATE jobs SET proposal_text = ?, proposal_drafted_at = ?, "
            "status = CASE WHEN status IN ('new','scored') THEN 'drafted' "
            "             ELSE status END "
            "WHERE id = ?",
            (text or "", now, job_id),
        )


# ── Profile ──────────────────────────────────────────────────────────────

DEFAULT_PROFILE = {
    "name": "",
    "headline": "",
    "skills": [],          # ["python", "ai agents", "browser automation"]
    "hourly_rate_usd": 0,  # what you charge
    "min_budget_fixed_usd": 0,   # filter — only score jobs ≥ this
    "preferred_niches": [],      # ["AI", "automation", "content"]
    "decline_list": [],          # ["wordpress", "logo design", ...]
    "languages": ["English"],
    "client_min_rating": 4.0,
    "client_payment_verified_only": True,
    "extra_context": "",   # free-form: user adds anything else helpful
}


def get_profile() -> dict:
    with _lock, _conn() as c:
        cur = c.execute("SELECT profile_json FROM user_profile WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        return dict(DEFAULT_PROFILE)
    try:
        out = json.loads(row["profile_json"])
        # Forward-compat: fill in any missing keys from defaults.
        merged = dict(DEFAULT_PROFILE)
        merged.update(out or {})
        return merged
    except Exception:
        return dict(DEFAULT_PROFILE)


def set_profile(profile: dict) -> None:
    now = datetime.utcnow().isoformat()
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO user_profile (id, profile_json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_json = excluded.profile_json,
                updated_at   = excluded.updated_at
            """,
            (json.dumps(profile or {}), now),
        )


# ── Stats ────────────────────────────────────────────────────────────────

def stats() -> dict:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        total = c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    return {
        "total":  total,
        "by_status": {r["status"]: r["n"] for r in rows},
    }
