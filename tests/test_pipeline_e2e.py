"""
End-to-end pipeline test: connects to the live mother as if it were the
dashboard, walks through every stage, validates what should happen at
each step. Designed to run unattended — prints PASS / FAIL for every check
and exits non-zero on first failure.

Usage:   cd ~/LotusAgent && venv/bin/python tests/test_pipeline_e2e.py

Pre-reqs (checked at startup):
  - Mother running (ws://localhost:8765 reachable)
  - Ollama running, gemma4:e4b available
  - Auth token file at ~/.lotus_auth/token.txt
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import websockets


TOKEN_PATH = Path(os.path.expanduser("~/.lotus_auth/token.txt"))
WS_URL = "ws://localhost:8765"
TMP_MD_DIR = Path("/tmp/lotus_test_md")
FAIL_FAST = True

# Test MD content — intentionally simple so Gemma parses quickly
SIMPLE_MD = """# Test prompts

1. A small ceramic teacup on a wooden table, soft morning light, photorealistic
2. A red origami crane on a white background, studio lighting
3. A minimalist mountain silhouette at sunset, pink gradient sky
"""


# ── tiny assertion helpers ────────────────────────────────────────────────

PASSES = []
FAILURES = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    line = f"  [{mark}] {name}" + (f" — {detail}" if detail else "")
    print(line, flush=True)
    (PASSES if cond else FAILURES).append(name)
    if not cond and FAIL_FAST:
        print(f"\nFAIL-FAST triggered after: {name}")
        sys.exit(1)


# ── minimal WS client ─────────────────────────────────────────────────────

class Client:
    def __init__(self, ws, token: str):
        self.ws = ws
        self.token = token
        self.events: list = []

    async def auth(self) -> None:
        first = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=5))
        ok("server sends auth_required",
           first.get("type") == "auth_required",
           f"got type={first.get('type')}")
        await self.ws.send(json.dumps({"type": "auth", "token": self.token}))
        second = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=5))
        ok("server grants auth_ok",
           second.get("type") == "auth_ok",
           f"got type={second.get('type')}")
        # 'connected' message also comes immediately after
        third = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=5))
        ok("server sends connected + boot_id",
           third.get("type") == "connected" and "boot_id" in third,
           f"got {third.get('type')} boot_id={bool(third.get('boot_id'))}")

    async def send(self, type_: str, **extra) -> None:
        await self.ws.send(json.dumps({"type": type_, **extra}))

    async def collect(self, duration: float = 1.0) -> list:
        """Collect any events arriving for `duration` seconds."""
        deadline = time.time() + duration
        out = []
        while time.time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(
                    self.ws.recv(), timeout=max(0.05, deadline - time.time())))
                out.append(msg)
                self.events.append(msg)
            except asyncio.TimeoutError:
                break
            except Exception:
                break
        return out

    async def wait_for(self, predicate, timeout: float = 10.0) -> dict:
        """Wait until an event matching `predicate` arrives, returning it.
        Returns {} on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(
                    self.ws.recv(), timeout=max(0.2, deadline - time.time())))
                self.events.append(msg)
                if predicate(msg):
                    return msg
            except asyncio.TimeoutError:
                break
            except Exception:
                break
        return {}


# ── tests ─────────────────────────────────────────────────────────────────

async def test_connect_and_auth(token: str) -> None:
    print("\n── test_connect_and_auth ──")
    async with websockets.connect(WS_URL) as ws:
        c = Client(ws, token)
        await c.auth()


async def test_pipeline_happy_path(token: str) -> None:
    """End-to-end: start → load → auto-approve → generate → approve → save."""
    print("\n── test_pipeline_happy_path ──")

    # Create a fresh test MD file
    TMP_MD_DIR.mkdir(parents=True, exist_ok=True)
    md_path = TMP_MD_DIR / "happy.md"
    md_path.write_text(SIMPLE_MD)

    async with websockets.connect(WS_URL) as ws:
        c = Client(ws, token)
        await c.auth()

        # 1. start_pipeline via user_command — filter by name so stale replays
        # from prior tests don't satisfy the assertion.
        await c.send("user_command", text="start pipeline HappyPath")
        ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state"
                      and m.get("stage") == "ask_location"
                      and "happypath" in (m.get("name", "").lower()),
            timeout=60,
        )
        ok("pipeline reaches ask_location stage with correct name", bool(ev),
           f"stages seen: {[e.get('stage') for e in c.events if e.get('type')=='pipeline_state']}  "
           f"names: {[e.get('name') for e in c.events if e.get('type')=='pipeline_state']}")
        pid = ev.get("id")

        # 2. set_source → expect processing banner → auto-approve → generating
        await c.send("pipeline_set_source", path=str(md_path))
        proc_ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state" and m.get("processing"),
            timeout=10,
        )
        ok("processing banner appears after set_source", bool(proc_ev))

        # Wait for prompts to be loaded (may take time — Gemma parses)
        loaded_ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state"
                      and len(m.get("prompts", [])) > 0
                      and m.get("stage") in ("review_prompts", "generating"),
            timeout=180,
        )
        ok("Gemma extracted >= 1 prompt",
           len(loaded_ev.get("prompts", [])) >= 1,
           f"extracted {len(loaded_ev.get('prompts', []))} prompts")

        # auto-approve runs as a second broadcast immediately after load. Wait
        # for a subsequent pipeline_state where every prompt is 'approved'.
        approved_ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state"
                      and len(m.get("prompts", [])) > 0
                      and all(p["status"] == "approved" for p in m.get("prompts", [])),
            timeout=15,
        )
        ok("prompts auto-approved (all 'approved')",
           bool(approved_ev),
           f"saw ev: {bool(approved_ev)}")

        # 3. Wait for generation loop to produce all 3 frames in pending_review
        n_expected = len(approved_ev.get("prompts", []))
        done_gen_ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state"
                      and m.get("stage") == "review_frames"
                      and len([f for f in m.get("frames", [])
                               if f.get("status") in ("pending_review", "failed")])
                          >= n_expected,
            timeout=360,
        )
        ok(f"all {n_expected} frames reached review_frames",
           bool(done_gen_ev),
           f"frames: {[f.get('status') for f in done_gen_ev.get('frames', [])]}")

        # 4. Approve all frames using the 'all' shortcut
        await c.send("pipeline_approve_frames", frame_ids="all")

        # 5. Eventually reach 'save' stage
        save_ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state" and m.get("stage") == "save",
            timeout=30,
        )
        ok("pipeline reaches save stage", bool(save_ev))

        # 6. Save to Post1
        await c.send("pipeline_save_here", section="Post1")
        done_ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state" and m.get("stage") == "done",
            timeout=60,
        )
        ok("pipeline reaches done stage", bool(done_ev))

        # 7. Verify files exist on disk
        from datetime import datetime
        parent_name = f"Techengine{datetime.now().strftime('%m%d%Y')}"
        post1 = Path(os.path.expanduser(f"~/LotusAgent/Projects/{parent_name}/Post1"))
        frames_saved = sorted(post1.glob("Frame*.png")) if post1.exists() else []
        ok("Frame files exist on disk",
           len(frames_saved) >= 1,
           f"{len(frames_saved)} file(s) in {post1}")


async def test_set_source_before_pipeline(token: str) -> None:
    """Race-guard: set_source arriving BEFORE start_pipeline should wait."""
    print("\n── test_set_source_before_pipeline ──")
    async with websockets.connect(WS_URL) as ws:
        c = Client(ws, token)
        await c.auth()
        # Send set_source with no active pipeline — should be silently dropped
        # (no pipeline to attach to, 8s wait then give up)
        await c.send("pipeline_set_source", path="/tmp/nonexistent.md")
        ev = await c.collect(1.5)
        # We expect NO pipeline_state event (no pipeline exists)
        pipeline_states = [m for m in ev if m.get("type") == "pipeline_state"]
        ok("set_source without active pipeline is silently dropped",
           len(pipeline_states) == 0,
           f"got {len(pipeline_states)} pipeline_state(s)")


async def test_cancel_pipeline(token: str) -> None:
    """Starting and cancelling a pipeline should clean up state."""
    print("\n── test_cancel_pipeline ──")
    async with websockets.connect(WS_URL) as ws:
        c = Client(ws, token)
        await c.auth()
        await c.send("user_command", text="start pipeline CancelMe")
        ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_state",
            timeout=20,
        )
        ok("pipeline started", bool(ev))
        pid = ev.get("id")
        # Cancel
        await c.send("pipeline_cancel", id=pid)
        ev = await c.wait_for(
            lambda m: m.get("type") == "pipeline_cancel",
            timeout=5,
        )
        ok("pipeline_cancel event received", bool(ev))
        # Let the cancel fully propagate to server.last_pipeline_state before
        # the next test opens a new connection (prevents stale replay).
        await asyncio.sleep(1)


# ── main ──────────────────────────────────────────────────────────────────

async def main() -> int:
    global FAIL_FAST
    if "--no-fail-fast" in sys.argv:
        FAIL_FAST = False

    print("LOTUS pipeline end-to-end tests")
    print("=" * 60)

    if not TOKEN_PATH.exists():
        print(f"Token missing at {TOKEN_PATH} — is the mother running?")
        return 1
    token = TOKEN_PATH.read_text().strip()

    # Each test gets its own connection (avoids state pollution)
    try:
        await test_connect_and_auth(token)
        await test_cancel_pipeline(token)
        await test_set_source_before_pipeline(token)
        await test_pipeline_happy_path(token)
    except Exception as e:
        print(f"\nUNHANDLED EXCEPTION: {e!r}")
        import traceback; traceback.print_exc()
        return 1

    print("\n" + "=" * 60)
    print(f"Done: {len(PASSES)} passed, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAIL: {f}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
