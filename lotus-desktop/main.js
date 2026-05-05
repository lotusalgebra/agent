// LOTUS Agent desktop client — thin Electron wrapper that points a native
// window at the mother's dashboard. No business logic lives here; clients
// are read-and-control UIs only. All decisions happen on the mother.

const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const SETTINGS_FILE = path.join(app.getPath('userData'), 'settings.json');
const TOKEN_FILE = path.join(os.homedir(), '.lotus_auth', 'token.txt');

// ── Bundled mother (PyInstaller-built `lotus-mother` from packaging/lotus-mother.spec)
//    In dev (npm start) the binary lives at ../dist/lotus-mother/lotus-mother
//    In a packaged .app it's copied to Contents/Resources/lotus-mother/lotus-mother
//    via electron-builder's `extraResources`. The mother owns ports 8765/8766
//    and persists its auth token at ~/.lotus_auth/token.txt — Electron just
//    spawns it, waits for the dashboard to come alive, and reads the token.

let motherProcess = null;

function motherBinaryPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'lotus-mother', 'lotus-mother');
  }
  return path.join(__dirname, '..', 'dist', 'lotus-mother', 'lotus-mother');
}

function spawnBundledMother() {
  const exe = motherBinaryPath();
  if (!fs.existsSync(exe)) {
    return { ok: false, error: `Bundled mother not found at ${exe}` };
  }
  motherProcess = spawn(exe, ['v'], {
    cwd: path.dirname(exe),
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: false,
  });
  motherProcess.stdout.on('data', (d) => {
    process.stdout.write(`[mother] ${d}`);
  });
  motherProcess.stderr.on('data', (d) => {
    process.stderr.write(`[mother:err] ${d}`);
  });
  motherProcess.on('exit', (code, signal) => {
    console.log(`[mother] exited code=${code} signal=${signal}`);
    motherProcess = null;
  });
  return { ok: true };
}

async function waitForMotherReady(timeoutMs = 60000) {
  // Poll the dashboard HTTP port until it responds. The mother takes ~5–10s
  // to clear preflight + Ollama check + voiceprint + dashboard bind, so a
  // 60s ceiling covers slow-boot edge cases (cold disk caches).
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch('http://localhost:8766/');
      if (r.status === 200 || r.status === 302) return true;
    } catch (_) { /* not ready yet */ }
    await new Promise((res) => setTimeout(res, 300));
  }
  return false;
}

function readMotherToken() {
  try {
    const raw = fs.readFileSync(TOKEN_FILE, 'utf8').trim();
    if (raw) return raw;
  } catch (_) { /* missing */ }
  return null;
}

// ── Settings persistence ────────────────────────────────────────────────

function loadSettings() {
  try {
    const raw = fs.readFileSync(SETTINGS_FILE, 'utf8');
    const s = JSON.parse(raw);
    if (s.serverHost && s.token) return s;
  } catch (_) { /* missing or invalid */ }
  return null;
}

function saveSettings(s) {
  fs.mkdirSync(path.dirname(SETTINGS_FILE), { recursive: true });
  fs.writeFileSync(SETTINGS_FILE, JSON.stringify(s, null, 2), { mode: 0o600 });
}

// ── Windows ─────────────────────────────────────────────────────────────

let mainWin = null;
let setupWin = null;

function dashboardUrl(settings) {
  const host = settings.serverHost;
  // New React UI served by Vite on :5173 (dev) or a bundled static server.
  // Legacy fallback: /dashboard.html at :8766 on the mother.
  const uiPort = settings.uiPort || 5173;
  const encToken = encodeURIComponent(settings.token);
  return `http://${host}:${uiPort}/?token=${encToken}`;
}

function openMainWindow(settings) {
  mainWin = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    title: 'LOTUS Agent',
    backgroundColor: '#050810',
    titleBarStyle: 'hiddenInset',      // macOS traffic-light style; ignored elsewhere
    trafficLightPosition: { x: 16, y: 18 },
    resizable: true,
    movable: true,
    maximizable: true,
    minimizable: true,
    fullscreenable: true,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),   // expose window.lotus.*
    },
  });
  mainWin.loadURL(dashboardUrl(settings));

  // Open external links in the user's default browser, not inside the app
  mainWin.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWin.on('closed', () => { mainWin = null; });
}

function openSetupWindow() {
  setupWin = new BrowserWindow({
    width: 440,
    height: 380,
    resizable: false,
    title: 'LOTUS Agent — Connect',
    backgroundColor: '#0a0f1c',
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  setupWin.loadFile(path.join(__dirname, 'setup.html'));
  setupWin.on('closed', () => { setupWin = null; });
}

// ── IPC: setup form submits here ────────────────────────────────────────

ipcMain.handle('lotus:save-settings', async (_evt, settings) => {
  if (!settings || !settings.serverHost || !settings.token) {
    return { ok: false, error: 'serverHost and token are required' };
  }
  saveSettings(settings);
  if (setupWin) setupWin.close();
  openMainWindow(settings);
  return { ok: true };
});

ipcMain.handle('lotus:clear-settings', async () => {
  try { fs.unlinkSync(SETTINGS_FILE); } catch (_) {}
  return { ok: true };
});

// Native file picker so the pipeline can prompt the user for a prompts.md
// file (or any other input) with macOS's standard Open dialog.
ipcMain.handle('lotus:pick-file', async (_evt, opts = {}) => {
  const win = BrowserWindow.getFocusedWindow() || mainWin;
  const result = await dialog.showOpenDialog(win, {
    title: opts.title || 'Select file',
    defaultPath: opts.defaultPath || undefined,
    filters: opts.filters || [
      { name: 'Markdown', extensions: ['md', 'markdown', 'txt'] },
      { name: 'All', extensions: ['*'] },
    ],
    properties: ['openFile'],
  });
  if (result.canceled || !result.filePaths.length) return { ok: false };
  return { ok: true, path: result.filePaths[0] };
});

// ── Splash window — shown during the ~10 s mother boot so the user has
//    immediate feedback after clicking the dock icon. Closed when the
//    dashboard window opens.

let splashWin = null;

function openSplashWindow() {
  splashWin = new BrowserWindow({
    width: 480,
    height: 280,
    frame: false,
    resizable: false,
    movable: true,
    alwaysOnTop: false,
    backgroundColor: '#050810',
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  const html = `
    <!doctype html><html><head><style>
      html, body { margin: 0; height: 100%; background: #050810;
        color: #e6f0ff; font: 14px -apple-system, system-ui, sans-serif;
        display: flex; align-items: center; justify-content: center; }
      .card { text-align: center; }
      .title { font-size: 20px; font-weight: 600; margin-bottom: 8px; }
      .sub { opacity: 0.65; font-size: 13px; }
      .dot { display: inline-block; width: 8px; height: 8px; margin: 0 3px;
        border-radius: 50%; background: #79b8ff;
        animation: p 1.4s infinite ease-in-out both; }
      .dot:nth-child(2) { animation-delay: 0.2s; }
      .dot:nth-child(3) { animation-delay: 0.4s; }
      @keyframes p { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }
    </style></head><body><div class="card">
      <div class="title">LOTUS Agent</div>
      <div class="sub">Booting mother — preflight, voiceprint, mic…</div>
      <div style="margin-top: 18px;">
        <span class="dot"></span><span class="dot"></span><span class="dot"></span>
      </div>
    </div></body></html>`;
  splashWin.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  splashWin.on('closed', () => { splashWin = null; });
}

// ── Bootstrap — spawns the bundled mother, waits for it, auto-configures
//    settings (host=localhost, token from ~/.lotus_auth/token.txt), then
//    opens the dashboard. Falls back to the legacy setup screen if the
//    user has saved settings pointing at a remote (non-localhost) mother.

async function bootstrap() {
  const existing = loadSettings();

  // Remote-mother mode: respect existing settings as before.
  if (existing && existing.serverHost && existing.serverHost !== 'localhost'
      && existing.serverHost !== '127.0.0.1') {
    openMainWindow(existing);
    return;
  }

  openSplashWindow();

  const spawnResult = spawnBundledMother();
  if (!spawnResult.ok) {
    if (splashWin) splashWin.close();
    dialog.showErrorBox(
      'LOTUS Agent — bundled mother missing',
      `${spawnResult.error}\n\nBuild it first:\n` +
      `  cd ~/LotusAgent && pyinstaller packaging/lotus-mother.spec --clean --noconfirm`
    );
    app.quit();
    return;
  }

  const ready = await waitForMotherReady(60000);
  if (!ready) {
    if (splashWin) splashWin.close();
    dialog.showErrorBox(
      'LOTUS Agent — mother failed to start',
      'The bundled mother did not come up within 60 s. Check the launcher ' +
      'log (Console.app → search "mother") for the failure reason.'
    );
    app.quit();
    return;
  }

  const token = readMotherToken();
  if (!token) {
    if (splashWin) splashWin.close();
    dialog.showErrorBox(
      'LOTUS Agent — token missing',
      `Could not read auth token at ${TOKEN_FILE}. The mother should have ` +
      `created it on first boot — check that ~/.lotus_auth is writable.`
    );
    app.quit();
    return;
  }

  const settings = {
    serverHost: 'localhost',
    httpPort: 8766,
    uiPort: 8766,
    token,
    bundledMother: true,
  };
  saveSettings(settings);
  openMainWindow(settings);
  if (splashWin) splashWin.close();
}

// ── Lifecycle ───────────────────────────────────────────────────────────

app.whenReady().then(() => { bootstrap(); });

let quittingMother = false;

app.on('before-quit', (event) => {
  if (motherProcess && !quittingMother) {
    event.preventDefault();
    quittingMother = true;
    const proc = motherProcess;
    const force = setTimeout(() => {
      try { proc.kill('SIGKILL'); } catch (_) {}
    }, 4000);
    proc.once('exit', () => {
      clearTimeout(force);
      app.quit();
    });
    try { proc.kill('SIGTERM'); } catch (_) { app.quit(); }
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    bootstrap();
  }
});
