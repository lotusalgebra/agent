"""
Smoke test for lotus_mcp + lotus_social against stand-in MCP servers.

Spawns tests/fake_social_mcp.py over real stdio (same transport as the
npx / uvx servers), so no credentials, network, or installs are needed.
The LLM is stubbed for deterministic checks; pass --live to also draft
with the real lotus_llm backend (Gemma without a key, Claude with one).

    source venv/bin/activate && python tests/test_social_mcp.py [--live]
"""

import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lotus_llm      # noqa: E402
import lotus_mcp      # noqa: E402
import lotus_social   # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok); FAIL += (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


tmp = Path(tempfile.mkdtemp(prefix="lotus_social_"))
reply_log = tmp / "replies.jsonl"
lotus_social.DB_PATH = tmp / "social.db"
lotus_social.REPORTS_DIR = tmp / "reports"
FAKE = str(ROOT / "tests" / "fake_social_mcp.py")
lotus_mcp.servers = lambda: {
    p: {"command": sys.executable, "args": [FAKE, p],
        "env": {"FAKE_SOCIAL_LOG": str(reply_log)},
        "required_env": [], "required_files": []}
    for p in ("instagram", "youtube")}

real_chat = lotus_llm.chat
calls = []


def stub_chat(messages, *, system=None, task="chat", json_mode=False, **kw):
    calls.append(task)
    if task == "audit":
        return "## Snapshot\n- reach 21000"
    payload = json.loads(messages[-1]["content"])
    out = []
    for c in payload["comments"]:
        spam = "followers" in c["text"].lower()
        out.append({"comment_id": c["comment_id"],
                    "reply": None if spam else f"Thanks {c['author']}!",
                    "why": "spam" if spam else "question"})
    return json.dumps({"replies": out})


lotus_llm.chat = stub_chat

print("\n── lotus_mcp ──")
with lotus_mcp.session("instagram", timeout=60) as s:
    names = {t["name"] for t in s.tools()}
    check("lists tools over stdio", "ig_get_comments" in names, f"{len(names)} tools")
    prof = s.call("ig_get_profile")
    check("decodes tool result", prof.get("username") == "techengine.lab")
    try:
        s.call("ig_fail")
        check("tool error raises MCPError", False)
    except lotus_mcp.MCPError as e:
        check("tool error raises MCPError", "token expired" in str(e), str(e)[:80])
    check("session survives an error", s.call("ig_get_profile")["id"] == "178")

saved = lotus_mcp.servers
lotus_mcp.servers = lambda: {"instagram": {**lotus_mcp.DEFAULT_SERVERS["instagram"],
                                           "env_file": "does-not-exist.env"}}
try:
    lotus_mcp.Session("instagram", timeout=10).start()
    check("missing creds -> MCPNotConfigured", False)
except lotus_mcp.MCPNotConfigured as e:
    check("missing creds -> MCPNotConfigured", "INSTAGRAM_ACCESS_TOKEN" in str(e))
lotus_mcp.servers = saved

print("\n── fetch_and_draft (instagram) ──")
counts = lotus_social.fetch_and_draft("instagram", progress=lambda *_: None)
check("counts", counts == {"media": 2, "new_comments": 3, "drafted": 2, "skipped": 1}, counts)
pend = {d["comment_id"]: d for d in lotus_social.list_drafts("pending")}
check("question drafted", "c1" in pend and pend["c1"]["draft"].startswith("Thanks"))
check("Hinglish comment drafted", "c5" in pend)
skipped = lotus_social.list_drafts("skipped")
check("spam skipped, not queued", [d["comment_id"] for d in skipped] == ["c2"])
allc = {d["comment_id"] for d in lotus_social.list_drafts(None)}
check("owner-answered + own comment ignored", not ({"c3", "c4"} & allc))
again = lotus_social.fetch_and_draft("instagram", progress=lambda *_: None)
check("re-fetch drafts nothing new", again["new_comments"] == 0, again)
check("nothing posted by fetch", not reply_log.exists())

print("\n── approve / reject ──")
d1 = pend["c1"]["id"]
try:
    lotus_social.approve(d1, "It's [WHICH SHORTCUT]!")
    check("unfilled [PLACEHOLDER] blocks posting", False)
except ValueError:
    check("unfilled [PLACEHOLDER] blocks posting",
          not reply_log.exists() and lotus_social.get_draft(d1)["status"] == "pending")
r = lotus_social.approve(d1, "Slide 3 is Cmd+Shift+5 - screen recording!")
posted = [json.loads(l) for l in reply_log.read_text().splitlines()]
check("approve posts edited text", posted == [{"tool": "ig_reply_to_comment",
      "comment_id": "c1", "message": "Slide 3 is Cmd+Shift+5 - screen recording!"}])
check("status posted + reply id", r["status"] == "posted" and r["reply_id"] == "reply_c1")
lotus_social.approve(d1)
check("second approve is a no-op",
      len(reply_log.read_text().splitlines()) == 1)
r = lotus_social.reject(pend["c5"]["id"])
check("reject", r["status"] == "rejected")
try:
    lotus_social.approve(pend["c5"]["id"])
    check("rejected draft can't be posted", False)
except ValueError:
    check("rejected draft can't be posted", True)

print("\n── youtube ──")
counts = lotus_social.fetch_and_draft("youtube", progress=lambda *_: None)
check("thread with replies skipped", counts["new_comments"] == 1, counts)
y = [d for d in lotus_social.list_drafts("pending") if d["platform"] == "youtube"][0]
check("HTML cleaned", y["comment_text"] == 'Great video!\nCan you test "Final Cut" next?',
      repr(y["comment_text"]))
r = lotus_social.approve(y["id"])
check("youtube reply posted", r["status"] == "posted" and r["reply_id"] == "yr_y1")

print("\n── audit ──")
a = lotus_social.audit("youtube")
check("audit report written", Path(a["path"]).exists() and "Snapshot" in a["report"])
data_sent = None


def capture(messages, **kw):
    global data_sent
    data_sent = json.loads(messages[-1]["content"])
    return "ok"


lotus_llm.chat = capture
lotus_social.audit("instagram")
check("IG audit gathers insights",
      data_sent["recent_media"][0]["insights"]["data"][0]["name"] == "reach")
lotus_llm.chat = capture
lotus_social.audit("youtube")
check("missing tool reported, not fatal", "error" in data_sent["top_shorts_28d"])
lotus_llm.chat = stub_chat

print("\n── HTTP hook ──")


class FakeHandler:
    def __init__(self, path, body=None, token=None):
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.code = None

    def send_response(self, code): self.code = code
    def send_header(self, *a): pass
    def end_headers(self): pass
    def json(self): return json.loads(self.wfile.getvalue())


token_file = Path.home() / ".lotus_auth" / "token.txt"
token = token_file.read_text().strip() if token_file.exists() else None
h = FakeHandler("/api/social/drafts")
lotus_social.handle_http(h, "GET")
check("no token -> 401", h.code == 401)
check("other paths untouched", lotus_social.handle_http(FakeHandler("/api/queue"), "GET") is False)
if token:
    h = FakeHandler("/api/social/drafts?status=all", token=token)
    lotus_social.handle_http(h, "GET")
    check("GET drafts", h.code == 200 and len(h.json()["drafts"]) == 4, h.code)
    lotus_mcp_cfg = lotus_mcp.configured
    lotus_mcp.configured = lambda p: True
    sk = lotus_social.list_drafts("skipped")[0]["id"]
    h = FakeHandler(f"/api/social/drafts/{sk}/approve", {"text": "Thanks!"}, token)
    lotus_social.handle_http(h, "POST")
    check("approve a skipped draft with text", h.code == 200
          and h.json()["draft"]["status"] == "posted", h.code)
    h = FakeHandler("/api/social/drafts/9999/reject", {}, token)
    lotus_social.handle_http(h, "POST")
    check("unknown draft -> 400", h.code == 400)
    lotus_mcp.configured = lotus_mcp_cfg
else:
    print("  (no ~/.lotus_auth/token.txt - authorized HTTP checks skipped)")

if "--live" in sys.argv:
    print(f"\n── live drafting via {lotus_llm.backend()} ──")
    lotus_llm.chat = real_chat
    lotus_social.DB_PATH = tmp / "live.db"
    c = lotus_social.fetch_and_draft("instagram", progress=print)
    print("  ", c)
    for d in lotus_social.list_drafts(None):
        print(f"   {d['status']:8s} @{d['author']}: {d['comment_text'][:50]!r}\n"
              f"            -> {d['draft']!r}  {d['error'] or ''}")
    check("live: spam not drafted",
          all(d["comment_id"] != "c2" or d["status"] == "skipped"
              for d in lotus_social.list_drafts(None)))
    check("live: questions drafted", c["drafted"] >= 2, c)

print(f"\n{'=' * 60}\nDone: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
