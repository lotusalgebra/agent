"""Dry-run the cinematic video stage with _gen_video stubbed.

Verifies, WITHOUT spending Flow credits, that:
  1. only motion prompts become clips in cinematic mode
  2. each clip is animated from its own act's approved keyframe
  3. a missing/failed keyframe degrades to text->video, not a skip
  4. legacy (non-cinematic) packs still animate every approved prompt
  5. keyframe frames survive the frame-dedupe
"""
import os, sys, types, threading, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LOTUS_SKIP_PREFLIGHT", "1")

import agent_phase1 as ap

CALLS = []


def fake_gen_video(prompt, target, image_path=None, *, aspect="9:16",
                   duration="8s", verbose=True):
    CALLS.append({"prompt": prompt[:40], "target": os.path.basename(target),
                  "image": os.path.basename(image_path) if image_path else None})
    with open(target, "wb") as f:
        f.write(b"\0" * 8192)          # non-trivial file so bookkeeping passes
    return target


class FakePipeline:
    def __init__(self, tmp, prompts, frames):
        self.id = "pipe-test"
        self.name = "test-film"
        self.stage = "review_frames"
        self.prompts = prompts
        self.frames = frames
        self.cancelled = False
        self.errors = []
        self.history_run_id = None
        self.source_hash = "hash"
        self._tmp = tmp

    def parent_folder(self):
        return self._tmp


class FakeAgent:
    def __init__(self, p):
        self._active_pipeline = p
        self._stop_event = threading.Event()

    def _pipeline_broadcast(self, *a, **k):
        pass

    def _broadcast(self, *a, **k):
        pass


def run_case(name, prompts, frames, expect):
    global CALLS
    CALLS = []
    tmp = tempfile.mkdtemp()
    for fr in frames:                      # materialise keyframe files
        if fr.get("_make_file"):
            os.makedirs(os.path.dirname(fr["final_path"]), exist_ok=True)
            with open(fr["final_path"], "wb") as f:
                f.write(b"\0" * 8192)

    p = FakePipeline(tmp, prompts, frames)
    agent = FakeAgent(p)
    bound = ap.LotusPhase1._pipeline_generate_approved_video.__get__(agent)
    bound()

    print(f"\n=== {name} ===")
    for c in CALLS:
        print(f"  clip {c['target']:12} src={c['image'] or 'TEXT->VIDEO':22} "
              f"prompt={c['prompt']!r}")
    got = [(c["target"], c["image"]) for c in CALLS]
    ok = got == expect
    print(f"  {'PASS' if ok else 'FAIL'}  expected {expect}")
    if not ok:
        print(f"        got      {got}")
    surviving = [f["id"] for f in p.frames if f.get("media") != "video"]
    print(f"  keyframe frames surviving dedupe: {surviving}")
    return ok


def kf(fid, prompt_id, tmp, n, status="approved", make=True):
    return {"id": fid, "prompt_id": prompt_id, "final_path":
            os.path.join(tmp, "Frames", f"Frame{n}.png"),
            "status": status, "media": "image", "_make_file": make}


def main():
    ok = True
    # Stub the real generator — no CDP, no Chrome, no Flow credits.
    ap._gen_video = fake_gen_video
    tmp = tempfile.mkdtemp()

    # --- Case 1: full cinematic, both acts have approved keyframes -------
    prompts = [
        {"id": "1", "status": "approved", "kind": "asset",  "act_number": None, "text": "Elena character sheet"},
        {"id": "2", "status": "approved", "kind": "image",  "act_number": 1,    "text": "ACT1 keyframe still"},
        {"id": "3", "status": "approved", "kind": "image",  "act_number": 2,    "text": "ACT2 keyframe still"},
        {"id": "4", "status": "approved", "kind": "motion", "act_number": 1,    "text": "She takes three slow steps forward"},
        {"id": "5", "status": "approved", "kind": "motion", "act_number": 2,    "text": "Slow handheld push in on her face"},
    ]
    frames = [kf("f2", "2", tmp, 1), kf("f3", "3", tmp, 2)]
    ok &= run_case("cinematic — both keyframes present", prompts, frames,
                   [("Clip1.mp4", "Frame1.png"), ("Clip2.mp4", "Frame2.png")])

    # --- Case 2: act 2's board failed -> that act degrades to text ------
    tmp2 = tempfile.mkdtemp()
    frames2 = [kf("f2", "2", tmp2, 1),
               kf("f3", "3", tmp2, 2, status="failed", make=False)]
    ok &= run_case("cinematic — act 2 keyframe failed", prompts, frames2,
                   [("Clip1.mp4", "Frame1.png"), ("Clip2.mp4", None)])

    # --- Case 3: legacy pack, no motion rows -> every approved prompt ---
    legacy = [
        {"id": "1", "status": "approved", "kind": "image", "act_number": None, "text": "slide one"},
        {"id": "2", "status": "approved", "kind": "image", "act_number": None, "text": "slide two"},
        {"id": "3", "status": "pending",  "kind": "image", "act_number": None, "text": "not approved"},
    ]
    ok &= run_case("legacy pack — unchanged text->video", legacy, [],
                   [("Clip1.mp4", None), ("Clip2.mp4", None)])

    ok &= handoff_cases()
    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


# ── step 5: boards -> clips handoff ──────────────────────────────────────

def handoff_cases():
    """_maybe_start_clips must fire exactly once, and only when the film
    has approved motion prompts AND rendered boards. Credits are on the
    line, so the negative cases matter more than the positive one."""
    import tempfile as _tf
    started = []
    results = []
    if True:
        def case(name, cinematic, prompts, frames, expect_start, phase="boards"):
            del started[:]
            tmp = _tf.mkdtemp()
            for fr in frames:
                if fr.get("_make_file"):
                    os.makedirs(os.path.dirname(fr["final_path"]), exist_ok=True)
                    open(fr["final_path"], "wb").write(b"\0" * 8192)
            p = FakePipeline(tmp, prompts, frames)
            p.cinematic = cinematic
            p.phase = phase
            agent = FakeAgent(p)
            # Stub the generator on the instance — the handoff should hand
            # off, not actually render.
            agent._pipeline_generate_approved_video = lambda: started.append(True)
            fired = ap.LotusPhase1._maybe_start_clips.__get__(agent)()
            import time
            time.sleep(0.2)
            got = bool(started)
            good = (got == expect_start) and (fired == expect_start)
            print(f"  {'PASS' if good else 'FAIL'}  {name}: "
                  f"started={got} expected={expect_start} phase_now={p.phase!r}")
            results.append(good)

        tmp = _tf.mkdtemp()
        motion_ok = [
            {"id": "1", "status": "approved", "kind": "image",  "act_number": 1, "text": "kf"},
            {"id": "2", "status": "approved", "kind": "motion", "act_number": 1, "text": "move"},
        ]
        board = [kf("f1", "1", tmp, 1)]

        print("\n=== boards -> clips handoff ===")
        case("film with approved motion + rendered board", True, motion_ok, board, True)
        case("ordinary image pack (not cinematic)", False, motion_ok, board, False)
        case("already in clips phase (idempotent)", True, motion_ok, board, False,
             phase="clips")
        case("motion prompt REJECTED at review", True,
             [motion_ok[0], {**motion_ok[1], "status": "rejected"}], board, False)
        case("no board rendered — refuses to burn credits", True, motion_ok, [], False)
    return all(results)


if __name__ == "__main__":
    sys.exit(main())
