# LOTUS Child Setup — Windows & macOS

This guide sets up a LOTUS **child** on a second machine. The child hosts a subset of agents (Parser, Review, Archive, optionally Renderer) and the mother (your Mac Studio) offloads work to it over the LAN.

**One-Ollama architecture:** children do NOT run their own Ollama. Every Gemma call (review, parse, summary) goes over the LAN to the mother's Ollama. This keeps GPU in one place and avoids double-installing models.

---

## Quick install — one-click per platform

Copy the LotusAgent folder to the child machine (AirDrop / USB / network share / `git clone`), then:

| Platform | Double-click |
|---|---|
| **macOS** | `install-child.command` |
| **Windows** | `install-child.bat` |

Each installer verifies prerequisites (Python 3.10+, Tesseract for OCR, Chrome if you want RendererAgent) then runs the cross-platform wizard `setup-child.py`. If a prereq is missing, it prints the exact install link / Homebrew command — you install it manually, re-run the installer.

**Why not auto-install?** Installing Python/Tesseract requires admin rights. We don't silently elevate; you approve each system change yourself.

The rest of this doc is the manual walkthrough — only read it if the one-click installer can't complete.

---

## 1. Manual path — prerequisites on the CHILD machine

| Requirement | Windows | macOS / Linux |
|---|---|---|
| **Python 3.10+** | Install from [python.org](https://www.python.org/downloads/windows/) — check *"Add python.exe to PATH"* at install time | Use the system Python, or `brew install python@3.12` |
| **Tesseract OCR** (for ReviewAgent) | [UB-Mannheim Tesseract build](https://github.com/UB-Mannheim/tesseract/wiki) — add install folder to PATH | `brew install tesseract` |
| **Git** (to clone the repo) | [git-scm.com](https://git-scm.com/download/win) | `brew install git` or built-in |
| **Google Chrome** (only if hosting RendererAgent) | installer | installer |

## 2. Get the LOTUS code on the child

```
git clone <mother-reachable-url> LotusAgent
cd LotusAgent
```

Or copy the `~/LotusAgent/` folder from the mother via AirDrop / USB drive / network share.

## 3. On the MOTHER — make Ollama LAN-accessible

Ollama defaults to `127.0.0.1`. Children can't reach that from another box. Pick one:

**macOS (mother) — set env and restart Ollama:**
```
launchctl setenv OLLAMA_HOST "0.0.0.0"
# then restart Ollama from the menubar app, or:
brew services restart ollama
```

Verify from the CHILD:
```
curl http://<mother-ip>:11434/api/tags
```
If you see JSON, the path is open. Find the mother's IP with `ifconfig | grep "inet "` on Mac.

## 4. Copy the auth token

The wizard will paste it into a prompt. Get it from the mother:
```
cat ~/.lotus_auth/token.txt
```
Copy the long random string.

## 5. Run the wizard on the CHILD

```
cd LotusAgent
python setup-child.py
```

It'll ask for:
- **Mother's host / IP** — e.g. `192.168.1.42` or `mac-studio.local`
- **Ollama URL** — auto-filled from the mother IP
- **Auth token** — paste from step 4
- **Which agents to host** — default is Parser + Review + Archive (the light ones). RendererAgent needs Chrome + Gemini login.
- **Port** — default 8770 (LAN-visible)

The wizard then:
1. Preflights Ollama reachability + port availability
2. Creates `.childvenv/` with the narrow deps (websockets, requests, Pillow, pytesseract, mistune)
3. Writes `~/.lotus_child_config.json`
4. Writes the auth token to `~/.lotus_auth/token.txt`
5. Generates a launcher:
   - Windows → `run-child.bat`
   - macOS   → `run-child.command` (double-clickable)
   - Linux   → `run-child.sh`

## 6. Firewall

**Windows Defender** will prompt on first bind. Allow *Private network* access.

If you never see the prompt, run (as admin in PowerShell):
```
netsh advfirewall firewall add rule name="LOTUS child" dir=in action=allow protocol=TCP localport=8770
```

**macOS** usually just prompts for Python network access — allow.

## 7. Start the child

Double-click the launcher, or from a terminal:
- Windows: `run-child.bat`
- macOS:   `./run-child.command`
- Linux:   `./run-child.sh`

You should see a banner:
```
================================================================
 LOTUS Agent Child
================================================================
  id:         25bedc03ee
  host:       WIN-DESKTOP (Windows 11)
  listen:     ws://0.0.0.0:8770
  mother:     192.168.1.42
  ollama_url: http://192.168.1.42:11434
  agents:     ['ParserAgent', 'ReviewAgent', 'ArchiverAgent']
================================================================
[child] ✓ Ollama reachable at http://192.168.1.42:11434  models: ['gemma4:e4b']
[child] ready — waiting for connections
```

Keep this terminal open — the child dies when you close it.

## 8. Back on the MOTHER — the child is auto-discovered

**You don't have to configure anything on the mother.** The child announces itself via mDNS (zeroconf) under `_lotus-agent._tcp.local.`. A mother running on the same LAN will auto-discover and verify the token fingerprint — if it matches, the child's agents are registered automatically.

Expected boot log on the mother:
```
[mdns] browsing for LOTUS children on the LAN…
[mdns] ↓ discovered <hostname> at <child-ip>:8770 hosting ['ParserAgent', 'ReviewAgent', 'ArchiverAgent']
[mdns]   registered ReviewAgent@<child-ip>:8770
[mdns]   registered ParserAgent@<child-ip>:8770
[mdns]   registered ArchiverAgent@<child-ip>:8770
```

And a controller alert fires in the dashboard: *"Child <hostname> (<ip>:port) joined · agents: …"*. When a child goes offline, a matching warning alert fires and its agents are deregistered automatically.

### Fallback — `LOTUS_CHILDREN` env var

If your LAN blocks mDNS (some enterprise networks do, or VLANs that drop 224.0.0.251:5353), fall back to explicit config:
```
LOTUS_CHILDREN="ws://<child-ip>:8770?agents=ParserAgent,ReviewAgent,ArchiverAgent" \
LOTUS_CDP_URL=http://localhost:9222 \
  venv/bin/python -u agent_phase1.py v
```
Both pathways coexist; explicit config takes precedence if both point at the same agent.

The registry's `pick_best()` automatically routes work to whichever provider (local or remote) is less busy at submit time.

---

## RendererAgent extra step (only if you chose it)

The RendererAgent needs its own Chrome with Gemini logged in on the child machine. The wizard prints the exact command. Walk-through:

**Windows:**
```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=%USERPROFILE%\.lotus_auth\chrome_cdp_profile https://gemini.google.com/app
```

**macOS:**
```
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.lotus_auth/chrome_cdp_profile" https://gemini.google.com/app &
```

Sign into Gemini in that Chrome window **ONCE**. The profile persists at `~/.lotus_auth/chrome_cdp_profile/`.

When you start the child, pass the CDP URL (the wizard handles this if you chose RendererAgent):
```
--cdp-url http://localhost:9222
```

Playwright install on child (only if RendererAgent is hosted):
```
.childvenv/bin/python -m pip install playwright playwright-stealth google-genai
.childvenv/bin/python -m playwright install chromium
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[child] ⚠ Ollama preflight FAILED` | Mother's Ollama isn't LAN-bound — see step 3. Double-check with `curl http://<mother-ip>:11434/api/tags` from the child. |
| Child launches but mother says `[child-link] auth failed` | Token mismatch. Copy the mother's `~/.lotus_auth/token.txt` to the child and re-run wizard. |
| `ModuleNotFoundError: websockets` | Re-run `setup-child.py` — the venv install didn't complete. |
| Windows can't find `tesseract` | Install the UB-Mannheim build and add its folder to your PATH. |
| RendererAgent says "browser launch failed" | The child process didn't get `LOTUS_CDP_URL` — confirm `run-child.bat` passes `--cdp-url`, or restart Chrome with the debug port. |

## Updating the child

When you pull new code on the mother, copy the same repo to the child (or `git pull`). The `.childvenv/` is kept — just re-run the launcher. Re-run `setup-child.py` only if deps change.
