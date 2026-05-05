# LOTUS — Productization plan

Saved 2026-05-01. Resume tomorrow with the three blocking decisions
at the bottom answered.

---

## The goal

Turn LOTUS from a personal Python project into **a sellable cross-platform
desktop app** (Mac + Windows). Strategy: sell agents to recover time/money
spent building this. Multi-agent product (Carousel maker, Reel, Upwork,
Photoshop watermark, etc.) — each agent either bundled or sold separately.

---

## What "real application" actually requires

| Layer | What it means | Cost |
|---|---|---|
| **Bundling** | Python backend bundled into Electron `.app` / `.exe`. One double-click to launch — no terminal, no `pkill`. | Free (electron-builder) |
| **Code-signing** | macOS: Apple Developer Program. Windows: EV certificate. Without these, OS blocks the installer. | $99/yr Apple + ~$300-500/yr MS EV |
| **Auto-update** | Squirrel.Mac / Squirrel.Windows. App checks for new versions, downloads, restarts. | Free (Electron's auto-updater) |
| **License system** | Key-based activation + machine-binding. Revokable. | Either DIY backend or Lemon Squeezy / Paddle / Gumroad (5-10% fee, saves months) |
| **Onboarding wizard** | First launch: install Ollama, pull model, link Chrome, set folders, paste license. Currently manual + terminal. | ~1-2 weeks UI work |
| **Marketing site** | Landing, demo video, pricing, checkout, docs. | $20-50/mo (Webflow/Framer) |
| **Support** | When customers email "broken", someone answers. Email is fine for first 50 customers. | Time |
| **Cross-platform testing** | Two OSes = ~2× the QA. Different Chrome paths, shells, sign tooling. | Time |

**Realistic solo timeline:** **3 months for sellable v1**, **6 months for polish across Mac+Windows.**

---

## Phased roadmap

### Phase 1 — Mac-only MVP (4-6 weeks)

Goal: First revenue. Sell to 5-10 beta customers at low price ($49-99).

- [ ] Bundle Python backend into `lotus-desktop/` Electron — one `.dmg`
- [ ] Onboarding wizard: Ollama install / model pull / Chrome link / first pipeline
- [ ] Apple Developer ID signing + notarization
- [ ] Lemon Squeezy checkout + license-key validation in app
- [ ] First-launch UI polish (replace `dashboard.html` legacy with the React `lotus-ui`)
- [ ] Beta-test docs / quick-start guide
- [ ] Land first 5 paying customers

### Phase 2 — Polish + Windows (4-6 weeks)

Goal: Cross-platform parity. 30+ paying customers.

- [ ] Windows installer (`.exe` / `.msi` via electron-builder)
- [ ] Windows code-signing
- [ ] Auto-update mechanism (Squirrel)
- [ ] Marketing site (landing + pricing + docs)
- [ ] Migrate to Stripe/Paddle if Lemon Squeezy outgrown
- [ ] Bug-bash from real users
- [ ] Customer support email + ticketing

### Phase 3 — Multi-agent platform (8-12 weeks)

Goal: Sell agents as add-ons / tiers.

- [ ] Plugin architecture in `agent_phase1.py` — agents register themselves
- [ ] Each agent (Carousel / Reel / Upwork / Photoshop) is a separately-licensable feature
- [ ] In-app store UI: browse / unlock / configure agents
- [ ] Subscription tier OR per-agent unlocks (depends on Q1 below)
- [ ] Agent SDK so power users (or you, in future) can build new agents

---

## Three blocking questions (answer tomorrow)

### Q1: Pricing model

- (a) **One-time purchase** — $99-$199 per license. Predictable revenue, churn isn't a thing, but no recurring income.
- (b) **Subscription** — $19/mo or $190/yr. Recurring revenue, but you owe customers ongoing updates and support. SaaS-grade.
- (c) **Freemium + Pro tier** — free tier limited (e.g. 1 pipeline at a time, watermark on outputs); Pro at $99 one-time or $19/mo unlocks. Best discovery, hardest to architect.

I'd recommend **(a) one-time $99** for the absolute fastest path to first revenue. Migrate to subscription later when you have a customer base + recurring update cadence.

### Q2: First product to sell

- (a) **"Lotus Carousel Maker"** — current content-production agent (parser → Gemini → review → save). The most complete, most demoable. Fastest to ship.
- (b) **"Lotus Studio"** — bundle of all agents (Carousel + Reel + Photoshop + Upwork). Bigger product but everything has to be polished.
- (c) Something narrower — pick one job, do it perfectly.

I'd recommend **(a)**. It's already 80% there. Ship that to first customers, then upsell into Studio in Phase 3.

### Q3: Distribution channel

- (a) **Direct download** — your own site, Lemon Squeezy checkout. You keep 90%, full control.
- (b) **Mac App Store** — discovery boost, Apple takes 15-30%. ⚠ MAS requires sandboxing, which **probably blocks Chrome automation entirely** — likely dealbreaker for Lotus.
- (c) **Setapp** — subscription bundle, $5-10/mo per app. ~30k subscribers. Apple-approved sandbox-free.

I'd recommend **(a)**. MAS sandbox kills Lotus's core. Setapp is a good Phase-2 add-on.

---

## My fastest-path recommendation

If you're overwhelmed, **answer Q1=(a) $99 / Q2=(a) Carousel Maker / Q3=(a) direct download** — that's the smallest sellable thing. Phase 1 ships in 4 weeks if you stay focused. First revenue ~$500-1000 from beta customers. Then we plan Phase 2.

---

## What ALREADY exists (what we're building on)

- `lotus-desktop/` — Electron skeleton (8 dirs, basic structure)
- `lotus-ui/` — Vite + React 19 + Tailwind dashboard (323 KB single-file, lots of features)
- `packaging/` — PyInstaller specs for Mac + Windows (`packaging/mac/`, `packaging/windows/`)
- `setup-mother.py` / `setup-child.py` — install scripts (CLI today)
- `install-mother.command` / `install-mother.bat` — bootstrappers
- `lotus-model.py` — Ollama model lifecycle
- `lotus_mdns.py` — LAN discovery for child agents
- Per-agent .py modules: `gemini_bot`, `gemini_api`, `gemini`, `grok_video`, `blender`, `photoshop`, `upwork_agent`

The skeleton is real. What's missing is **the productization layer on top**: bundling, signing, licensing, onboarding, marketing.

---

## Next session, I'll do this

When you come back tomorrow with Q1/Q2/Q3 answered:

1. Lock the Phase 1 plan to a concrete week-by-week breakdown
2. Set up the Apple Developer account flow (you do that part)
3. Draft the Lemon Squeezy product config + landing copy
4. Start the Electron bundling (the first technical task)
