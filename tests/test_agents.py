"""
Smoke tests for the LOTUS agent mesh.

Each test is standalone, fast, and runs agents in isolation so we catch
protocol bugs before any pipeline wiring happens. Heavy paths (actual
RendererAgent → Gemini round-trip) are covered separately by the e2e
pipeline tests; here we only verify the protocol + the cheap agents.

Pre-reqs:
  - Ollama running locally, gemma4:e4b present
  - pytesseract installed + tesseract binary (Phase 2 install)
  - At least one saved image exists under the current projects_root
    (skips ReviewAgent test if not)

Usage:  venv/bin/python tests/test_agents.py
"""

from __future__ import annotations
import os
import sys
import time
from pathlib import Path


# Ensure we can import agents.py from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASSES: list = []
FAILURES: list = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    (PASSES if cond else FAILURES).append(name)


# ── 1. BaseAgent protocol — pure unit, no externals ──

def test_base_agent_contract() -> None:
    print("\n── test_base_agent_contract ──")
    from agents import BaseAgent

    class EchoAgent(BaseAgent):
        def __init__(self): super().__init__("EchoAgent")
        def run(self, task): return {"echo": task.get("payload")}

    class BoomAgent(BaseAgent):
        def __init__(self): super().__init__("BoomAgent")
        def run(self, task): raise ValueError("intentional")

    echo = EchoAgent()
    wid = echo.submit({"payload": "hi"})
    ok("submit returns a work_id string", isinstance(wid, str) and len(wid) > 0, wid)

    # Poll until done (should be near-instant).
    deadline = time.time() + 3
    while time.time() < deadline:
        s = echo.status(wid)
        if s["state"] in ("done", "failed"): break
        time.sleep(0.05)
    s = echo.status(wid)
    ok("echo work reaches state=done", s["state"] == "done", f"state={s['state']}")
    ok("echo result payload echoed back",
       s.get("result", {}).get("echo") == "hi",
       f"result={s.get('result')}")

    h = echo.health()
    ok("health reports success_count=1", h["success_count"] == 1, str(h))

    # Failure path
    boom = BoomAgent()
    wid = boom.submit({})
    deadline = time.time() + 3
    while time.time() < deadline:
        if boom.status(wid)["state"] in ("done", "failed"): break
        time.sleep(0.05)
    s = boom.status(wid)
    ok("boom work reaches state=failed", s["state"] == "failed", f"state={s['state']}")
    ok("boom error captured and prefixed with type",
       (s.get("error") or "").startswith("ValueError:"),
       f"error={s.get('error')}")

    h = boom.health()
    ok("boom health fail_count=1", h["fail_count"] == 1, str(h))


# ── 2. ParserAgent — needs Ollama + prompts_parser.mistune ──

def test_parser_agent() -> None:
    print("\n── test_parser_agent ──")
    # Build a tiny MD file that the parser should handle cleanly.
    import tempfile
    md = "# Test prompts\n\n1. A red origami crane on a white background, studio lighting\n2. A small ceramic teacup on a wooden table, soft morning light\n"
    td = Path(tempfile.mkdtemp(prefix="lotus_agents_"))
    md_path = td / "tiny.md"
    md_path.write_text(md)

    from agents import ParserAgent
    pa = ParserAgent()
    wid = pa.submit({"md_path": str(md_path)})
    deadline = time.time() + 120
    while time.time() < deadline:
        s = pa.status(wid)
        if s["state"] in ("done", "failed"): break
        time.sleep(0.5)
    s = pa.status(wid)
    if s["state"] != "done":
        ok("parser completes without error",
           False, f"state={s['state']}  error={s.get('error')}")
        return
    ok("parser completes", True, f"elapsed={s['elapsed']}s")
    result = s.get("result") or {}
    ok("parser extracts >= 1 prompt",
       result.get("count", 0) >= 1,
       f"count={result.get('count')}")


# ── 3. ReviewAgent — needs pytesseract + Ollama ──

def test_review_agent() -> None:
    print("\n── test_review_agent ──")

    # Find ANY saved image on disk to test against. Skip if none.
    try:
        import lotus_config as _lc
        root = os.path.expanduser(_lc.load()["image"].get(
            "projects_root", "~/LotusAgent/Projects"))
    except Exception:
        root = os.path.expanduser("~/LotusAgent/Projects")
    image = None
    if os.path.isdir(root):
        for dp, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".png", ".jpg", ".jpeg")):
                    full = os.path.join(dp, f)
                    if os.path.getsize(full) > 50 * 1024:  # skip tiny broken files
                        image = full; break
            if image: break
    if not image:
        ok("ReviewAgent — found a test image", False,
           "no image found under projects_root; skipping")
        return

    from agents import ReviewAgent
    ra = ReviewAgent()
    synthetic_prompt = (
        "Photorealistic car photograph, warm gold highlight label at top "
        "reading 'LUXURY', white drop-shadowed description below, "
        "@techengine.lab brand mark in the bottom-left corner."
    )
    wid = ra.submit({"image_path": image, "prompt_text": synthetic_prompt})
    deadline = time.time() + 120
    while time.time() < deadline:
        s = ra.status(wid)
        if s["state"] in ("done", "failed"): break
        time.sleep(0.5)
    s = ra.status(wid)
    if s["state"] != "done":
        ok("review completes without error",
           False, f"state={s['state']}  error={s.get('error')}")
        return
    ok("review completes", True, f"elapsed={s['elapsed']}s")
    r = s.get("result") or {}
    ok("review result has 'match' key",
       "match" in r,
       f"result={r}")
    ok("review result has a 'reason' string",
       isinstance(r.get("reason"), str) and len(r["reason"]) > 0,
       f"reason={r.get('reason')!r}")
    ok("review result reports had_vision=True",
       r.get("had_vision") is True,
       f"had_vision={r.get('had_vision')}")
    # text_match and vision_match may be None if Gemma didn't emit them
    # but the call completed — don't hard-fail on those, just report.
    print(f"    image:        {image}")
    print(f"    match:        {r.get('match')}")
    print(f"    text_match:   {r.get('text_match')}")
    print(f"    vision_match: {r.get('vision_match')}")
    print(f"    reason:       {r.get('reason')}")
    print(f"    ocr_chars:    {r.get('ocr_chars')}")


# ── 4. ArchiverAgent — needs Ollama (for summary) + write access ──

def test_archiver_agent() -> None:
    print("\n── test_archiver_agent ──")
    from agents import ArchiverAgent
    import tempfile
    reports_dir = Path(tempfile.mkdtemp(prefix="lotus_reports_"))
    # Synthetic pipeline dict (mirrors Pipeline.as_event()'s shape).
    pipeline = {
        "id": "abc123",
        "name": "SmokeTest",
        "stage": "done",
        "source_md": "/tmp/test.md",
        "prompts": [{"id":"p1","text":"A crane","status":"approved"}],
        "frames": [
            {"id":"f1","status":"approved","source":"lotus"},
            {"id":"f2","status":"pending_review","source":"manual"},
            {"id":"f3","status":"failed","source":None,
             "error":"Review rejected: missing brand"},
            {"id":"f4","status":"skipped","source":"skipped"},
        ],
        "errors": [
            {"ts": time.time(), "frame_id":"f3","section":"Post1",
             "stage":"review","message":"Missing brand mark"},
        ],
        "created_at": time.time() - 600,
    }
    arc = ArchiverAgent()
    wid = arc.submit({
        "pipeline": pipeline,
        "reports_dir": str(reports_dir),
        "ask_gemma": True,
    })
    deadline = time.time() + 120
    while time.time() < deadline:
        if arc.status(wid)["state"] in ("done", "failed"): break
        time.sleep(0.3)
    s = arc.status(wid)
    if s["state"] != "done":
        ok("archiver completes without error", False,
           f"state={s['state']} error={s.get('error')}")
        return
    ok("archiver completes", True, f"elapsed={s['elapsed']}s")
    r = s.get("result") or {}
    report_path = r.get("report_path", "")
    ok("report file written",
       report_path and os.path.exists(report_path),
       report_path)
    counts = r.get("counts", {})
    ok("counts include by_lotus",   counts.get("by_lotus")   == 1, f"counts={counts}")
    ok("counts include manual",     counts.get("manual")     == 1, "")
    ok("counts include failed",     counts.get("failed")     == 1, "")
    ok("counts include skipped",    counts.get("skipped")    == 1, "")
    ok("counts include total",      counts.get("total")      == 4, "")
    summary = r.get("summary", "") or ""
    ok("Gemma summary non-empty",   len(summary) > 20,
       f"len={len(summary)}  preview={summary[:80]}")
    print(f"    summary: {summary}")


# ── 5. Registry sanity ──

def test_default_registry() -> None:
    print("\n── test_default_registry ──")
    from agents import default_registry
    snap = default_registry.health_snapshot()
    ok("registry has exactly 4 agents",
       len(snap) == 4,
       f"got {len(snap)}: {[a['name'] for a in snap]}")
    names = {a["name"] for a in snap}
    for n in ("ParserAgent", "RendererAgent", "ReviewAgent", "ArchiverAgent"):
        ok(f"registry includes {n}", n in names, "")


# ── main ──

def main() -> int:
    print("LOTUS agent-mesh smoke tests")
    print("=" * 60)
    test_base_agent_contract()
    test_default_registry()
    test_parser_agent()
    test_review_agent()
    test_archiver_agent()
    print("\n" + "=" * 60)
    print(f"Done: {len(PASSES)} passed, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAIL: {f}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
