"""Upwork management agent — scrapes the Best Matches feed via the
user's CDP-attached real Chrome, scores jobs with local Gemma, can hand
off to Claude.ai (browser) for proposal drafting.

Architecture mirrors gemini_bot/grok_video: connects via LOTUS_CDP_URL to
a real signed-in Chrome (Upwork blocks Playwright Chromium aggressively;
real Chrome with the user's session is the only durable path).

Public API:
    scrape_feed(*, limit=20)              → list[job_dict]
    score_jobs(jobs, profile)             → list[job_dict] with score+reason
    refresh_feed(*, limit=20, score=True) → list[stored job rows]
    draft_proposal(job_id)                → drafted proposal text
    list_top(*, limit=10, min_score=0.7)  → list[stored rows]

Selectors are best-effort — Upwork's class names are obfuscated and
churn frequently. We use semantic selectors (article, [data-test=...],
role=heading) where possible and fall back to text-pattern matching.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional

import requests
from playwright.async_api import async_playwright

import upwork_db as db


_FEED_URL = "https://www.upwork.com/nx/find-work/best-matches"
_DEFAULT_TIMEOUT_MS = 30_000
_VERBOSE = True

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
GEMMA_MODEL = "gemma4:e4b"


# ── single dedicated event loop (mirrors grok_video pattern) ──────────────

_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_pw = None
_browser = None
_context = None
_page = None


def _start_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(
            target=_loop.run_forever, name="upwork-agent-loop", daemon=True)
        _thread.start()
    return _loop


async def _ensure_page(verbose: bool = _VERBOSE):
    """Connect via CDP and surface a page on the Upwork feed."""
    global _pw, _browser, _context, _page
    if _page:
        try:
            _ = _page.url
            if "upwork.com" not in (_page.url or ""):
                await _page.goto(_FEED_URL, wait_until="domcontentloaded",
                                 timeout=_DEFAULT_TIMEOUT_MS)
            return _page
        except Exception:
            _page = None

    if _pw is None:
        _pw = await async_playwright().start()
    # Upwork runs in its OWN Chrome — sharing the main Lotus CDP profile
    # with Gemini/Grok caused Upwork's bot-detection to bounce sessions.
    # `LOTUS_UPWORK_CDP_URL` points at a dedicated Chrome (e.g. port 9223
    # with chrome_upwork_profile). Falls back to the main CDP if not set.
    cdp_url = (os.environ.get("LOTUS_UPWORK_CDP_URL")
               or os.environ.get("LOTUS_CDP_URL"))
    if not cdp_url:
        raise RuntimeError(
            "upwork_agent needs LOTUS_UPWORK_CDP_URL (or LOTUS_CDP_URL "
            "fallback). Launch a Chrome with --remote-debugging-port=9223 "
            "and a dedicated user-data-dir for Upwork.")
    if verbose:
        print(f"[upwork] CDP attach at {cdp_url}", flush=True)
    _browser = await _pw.chromium.connect_over_cdp(cdp_url)
    if _browser.contexts:
        _context = _browser.contexts[0]
    else:
        _context = await _browser.new_context(accept_downloads=True)
    candidate = None
    for p in _context.pages:
        try:
            if "upwork.com" in (p.url or ""):
                candidate = p; break
        except Exception:
            pass
    if candidate is None:
        candidate = await _context.new_page()
    if "upwork.com" not in (candidate.url or "") or "find-work" not in (candidate.url or ""):
        await candidate.goto(_FEED_URL, wait_until="domcontentloaded",
                             timeout=_DEFAULT_TIMEOUT_MS)
    candidate.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    _page = candidate
    return _page


# ── scraping ─────────────────────────────────────────────────────────────

_SCRAPE_JS = """
async (limit) => {
    // Wait until the feed has rendered something useful.
    function waitFor(ms) { return new Promise(r => setTimeout(r, ms)); }
    let tries = 30;
    while (tries-- > 0) {
        const tiles = document.querySelectorAll('article[data-test="JobTile"], section[data-test="JobTile"], [data-test="job-tile-list"] article, [data-ev-job-uid]');
        if (tiles.length > 0) break;
        await waitFor(500);
    }
    const out = [];
    // Multiple shapes Upwork has used over time — try them in order.
    const tileSelectors = [
        'article[data-test="JobTile"]',
        'section[data-test="JobTile"]',
        '[data-test="job-tile-list"] article',
        '[data-ev-job-uid]',
    ];
    let tiles = [];
    for (const sel of tileSelectors) {
        tiles = document.querySelectorAll(sel);
        if (tiles.length) break;
    }
    for (let i = 0; i < tiles.length && i < limit; i++) {
        const t = tiles[i];
        const titleA = t.querySelector('a[data-test="job-tile-title-link"], a.job-tile-title-link, h3 a, h2 a');
        const titleText = (titleA && titleA.innerText || '').trim();
        const url = titleA ? new URL(titleA.getAttribute('href') || '', location.origin).href : '';

        // Pull a stable id from the URL (..._~012abc...).
        let id = '';
        const m = url.match(/_~([0-9a-zA-Z]+)/);
        if (m) id = m[1];
        if (!id) id = t.getAttribute('data-ev-job-uid') || '';
        if (!id && url) id = url.split('/').pop();

        const description = (t.querySelector('[data-test="UpCLineClamp JobDescription"], [data-test="JobDescription"], .job-description, p[data-test="job-description-text"]') || {}).innerText || '';
        const budget      = (t.querySelector('[data-test="job-type-label"], [data-test="is-fixed-price"], [data-test="budget"]') || {}).innerText || '';
        const posted_at   = (t.querySelector('[data-test="job-pubilshed-date"], small[data-test="posted-on"], time') || {}).innerText || '';
        const country     = (t.querySelector('[data-test="client-country"]') || {}).innerText || '';
        const spent       = (t.querySelector('[data-test="client-spendings"], [data-test="total-spent"]') || {}).innerText || '';
        const ratingEl    = t.querySelector('[data-test="UpCRating"], [data-test="rating"]');
        const rating      = ratingEl ? parseFloat((ratingEl.getAttribute('aria-label') || ratingEl.innerText || '').match(/[\\d.]+/)?.[0] || '') : null;

        out.push({
            id, url, title: titleText,
            description: description.trim(),
            budget: budget.trim(),
            posted_at: posted_at.trim(),
            client_country: country.trim() || null,
            client_spent:   spent.trim()   || null,
            client_rating:  rating || null,
        });
    }
    return out;
}
"""


async def _scrape_feed_async(*, limit: int = 20,
                              verbose: bool = _VERBOSE) -> list:
    page = await _ensure_page(verbose=verbose)
    if "upwork.com" not in (page.url or ""):
        await page.goto(_FEED_URL, wait_until="domcontentloaded",
                        timeout=_DEFAULT_TIMEOUT_MS)
    # Trigger a fresh DOM read by waiting for network idle (best-effort).
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception: pass
    raw = await page.evaluate(_SCRAPE_JS, limit)
    now = datetime.utcnow().isoformat()
    jobs = []
    for j in raw:
        if not j.get("id") or not j.get("title"):
            continue
        # Heuristic: budget_type from the budget string.
        b = (j.get("budget") or "").lower()
        if "/hr" in b or "hour" in b:
            j["budget_type"] = "hourly"
        elif "$" in b or "fixed" in b:
            j["budget_type"] = "fixed"
        j["fetched_at"] = now
        j["raw"] = {"feed_html_at": now}
        jobs.append(j)
    if verbose:
        print(f"[upwork] scraped {len(jobs)} job(s) from feed", flush=True)
    return jobs


def scrape_feed(*, limit: int = 20, verbose: bool = _VERBOSE) -> list:
    """Sync entry. Returns raw job dicts (NOT yet scored)."""
    loop = _start_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _scrape_feed_async(limit=limit, verbose=verbose), loop)
    return fut.result(timeout=120)


# ── scoring (Gemma) ──────────────────────────────────────────────────────

_SCORE_SYSTEM = (
    "You are a fit-scoring assistant for an Upwork freelancer. Given a "
    "freelancer profile and a single job posting, return a fit score 0.0-1.0 "
    "and a one-sentence reason. Score reflects: (1) skill match, (2) budget "
    "matches the freelancer's expectations, (3) the work fits the niches the "
    "freelancer wants and is NOT in their decline list, (4) client looks "
    "credible (rating, spend, country preference if any). High scores >= 0.8 "
    "are strong fits; 0.5-0.79 worth a look; <0.5 skip.\n\n"
    'Reply JSON ONLY: {"score": 0.0-1.0, "reason": "<short>"}'
)


def _score_one(job: dict, profile: dict, *,
               timeout: int = 25) -> tuple:
    """Returns (score, reason). Falls back to (None, '<error>') on any
    Gemma failure — caller is responsible for skipping unscored rows."""
    user_payload = {
        "freelancer": {
            "skills": profile.get("skills") or [],
            "hourly_rate_usd": profile.get("hourly_rate_usd") or 0,
            "min_budget_fixed_usd": profile.get("min_budget_fixed_usd") or 0,
            "preferred_niches": profile.get("preferred_niches") or [],
            "decline_list": profile.get("decline_list") or [],
            "languages": profile.get("languages") or [],
            "client_min_rating": profile.get("client_min_rating") or 0,
            "extra_context": profile.get("extra_context") or "",
        },
        "job": {
            "title":        job.get("title") or "",
            "description":  (job.get("description") or "")[:1500],
            "budget":       job.get("budget") or "",
            "budget_type":  job.get("budget_type") or "",
            "client_country": job.get("client_country"),
            "client_rating": job.get("client_rating"),
            "client_spent":  job.get("client_spent"),
        },
    }
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":   GEMMA_MODEL,
                "stream":  False,
                "format":  "json",
                "options": {"temperature": 0.0, "num_predict": 150,
                            "num_ctx": 4096},
                "messages": [
                    {"role": "system", "content": _SCORE_SYSTEM},
                    {"role": "user",   "content": json.dumps(user_payload)},
                ],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        body = (r.json().get("message") or {}).get("content", "").strip()
        parsed = json.loads(body)
        s = float(parsed.get("score", 0.0))
        s = max(0.0, min(1.0, s))
        return (s, str(parsed.get("reason", ""))[:200])
    except Exception as e:
        return (None, f"score-error: {type(e).__name__}")


def score_jobs(jobs: list, profile: Optional[dict] = None,
               *, verbose: bool = _VERBOSE) -> list:
    """Score each job in-place (mutates the dicts). Returns the list."""
    profile = profile if profile is not None else db.get_profile()
    for j in jobs:
        score, reason = _score_one(j, profile)
        j["score"] = score
        j["score_reason"] = reason
        if verbose:
            mark = f"{score:.2f}" if score is not None else "  ?"
            print(f"[upwork] {mark}  {j.get('title','')[:60]}",
                  flush=True)
    return jobs


# ── public orchestrators ─────────────────────────────────────────────────

def refresh_feed(*, limit: int = 20, score: bool = True,
                 verbose: bool = _VERBOSE) -> list:
    """Scrape, persist, optionally score, return updated rows."""
    jobs = scrape_feed(limit=limit, verbose=verbose)
    for j in jobs:
        db.upsert_job(j)
    if score and jobs:
        profile = db.get_profile()
        score_jobs(jobs, profile, verbose=verbose)
        for j in jobs:
            if j.get("score") is not None:
                db.set_job_score(j["id"], j["score"], j.get("score_reason") or "")
    return db.list_jobs(limit=limit)


def list_top(*, limit: int = 10, min_score: float = 0.7) -> list:
    return db.list_jobs(min_score=min_score, limit=limit)


# ── proposal drafting (Claude.ai stub for now) ───────────────────────────

def _draft_via_gemma(job: dict, profile: dict, *,
                     timeout: int = 60) -> Optional[str]:
    """Draft via local Gemma — fallback when Claude.ai isn't reachable.
    Quality is lower but works fully offline. The Claude.ai browser path
    is the next iteration."""
    sys_prompt = (
        "You are writing a personalized Upwork proposal for the "
        "freelancer based on the job below. Voice: confident, specific, "
        "lead with the most relevant past work, end with a single "
        "concrete next-step question. 150–220 words. NO bullet lists — "
        "two short paragraphs. Reference the job's actual ask, don't "
        "use canned phrases like 'I'm the perfect fit'."
    )
    user_payload = {
        "freelancer": profile,
        "job": {
            "title":       job.get("title"),
            "description": job.get("description"),
            "budget":      job.get("budget"),
        },
    }
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":   GEMMA_MODEL,
                "stream":  False,
                "options": {"temperature": 0.3, "num_predict": 600,
                            "num_ctx": 4096},
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user",   "content": json.dumps(user_payload)},
                ],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        body = (r.json().get("message") or {}).get("content", "").strip()
        return body if body else None
    except Exception as e:
        print(f"[upwork] gemma draft error: {e}", flush=True)
        return None


def draft_proposal(job_id: str, *, verbose: bool = _VERBOSE) -> Optional[str]:
    """V1: drafts via local Gemma. V2 will route to Claude.ai browser."""
    job = db.get_job(job_id)
    if not job:
        return None
    text = _draft_via_gemma(job, db.get_profile())
    if text:
        db.set_job_proposal(job_id, text)
    if verbose:
        print(f"[upwork] draft for {job_id}: "
              f"{(text or '')[:80]}…", flush=True)
    return text


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python upwork_agent.py scrape [limit=10]")
        print("  python upwork_agent.py refresh [limit=10]")
        print("  python upwork_agent.py top [min=0.7] [limit=10]")
        print("  python upwork_agent.py draft <job_id>")
        print("  python upwork_agent.py profile [--show | --set JSON]")
        sys.exit(1)
    sub = sys.argv[1]
    if sub == "scrape":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        out = scrape_feed(limit=n)
        print(json.dumps(out, indent=2)[:4000])
    elif sub == "refresh":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        rows = refresh_feed(limit=n)
        print(json.dumps([{k: r[k] for k in ("id","title","budget","score","score_reason","status")} for r in rows], indent=2))
    elif sub == "top":
        m = float(sys.argv[2]) if len(sys.argv) > 2 else 0.7
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        rows = list_top(min_score=m, limit=n)
        for r in rows:
            print(f"  {r['score']:.2f}  {r['title'][:60]}")
            print(f"        {r['score_reason']}")
            print(f"        {r['url']}")
    elif sub == "draft":
        if len(sys.argv) < 3:
            print("need job_id"); sys.exit(1)
        text = draft_proposal(sys.argv[2])
        print(text or "(no draft)")
    elif sub == "profile":
        if len(sys.argv) > 2 and sys.argv[2] == "--set":
            db.set_profile(json.loads(sys.argv[3]))
        print(json.dumps(db.get_profile(), indent=2))
    else:
        print("unknown subcommand"); sys.exit(1)
