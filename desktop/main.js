const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn, execFileSync } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');

// The GT 710 (Kepler) GPU is unsupported by modern Chromium/Electron and
// causes renderer hangs/blank windows. Disable hardware acceleration so the
// UI stays responsive on this machine.
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('disable-gpu');

// Single instance: the portable launcher can re-run the same app; a second
// instance would spawn a second backend and both would write knowledge.json.
// If we lose the lock, tell the winner to focus and exit quietly.
if (!app.requestSingleInstanceLock()) {
    app.quit();
}
app.on('second-instance', () => {
    if (mainWindow) {
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.show();
        mainWindow.focus();
    }
});

const PORT = 5000;
const PROJECT_DIR = path.resolve(__dirname, '..');
const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';
function exeName(bin) { return IS_WIN ? `${bin}.exe` : bin; }

// In a packaged build electron-builder extracts the Python venv + server folder
// to process.resourcesPath. In dev they live in the project folder. Handle the
// platform-specific venv layout (Scripts/ on Windows, bin/ on Unix).
let PYTHON_CANDIDATES = [
    path.join(PROJECT_DIR, 'venv', 'Scripts', exeName('python')),
    'python3',
    'C:\\Users\\User\\Python312\\python.exe',
];
let SERVER_DIR = path.join(PROJECT_DIR, 'server');

// Public release builds are marked with a `.public` file next to the backend
// (written by `node prepare-backend.js --public`). They strip the trainer/admin
// UI, never verify a disc, and lock training-control endpoints server-side.
const PUBLIC_BUILD = app.isPackaged &&
    fs.existsSync(path.join(process.resourcesPath, 'backend', '.public'));

// Copy-protection (optional, Windows-only): the app locks until a verification
// file is read from an optical/removable drive. It only arms on machines that
// actually have an optical drive, and can be force-disabled for open/public
// installs with GALAXYPRON_DISABLE_VERIFY=1. Non-Windows builds never verify.
const VERIFY_FILENAME = 'galaxypron.verify';
const VERIFY_TOKEN = 'GALAXYPRON-VERIFY-7A37B03C7A9942278E228F48';
const VERIFY_ARMED = app.isPackaged && IS_WIN && !PUBLIC_BUILD &&
    process.env.GALAXYPRON_DISABLE_VERIFY !== '1';

// Internal admin key shared with the backend. The packaged build sets it so the
// training-control endpoints reject anything that isn't signed by this shell.
const ADMIN_KEY = 'galaxypron-' + app.getVersion() + '-internal-admin-7A37B';

if (app.isPackaged) {
    PYTHON_CANDIDATES = [
        path.join(process.resourcesPath, 'backend', 'venv', 'Scripts', exeName('python')),
        path.join(process.resourcesPath, 'backend', 'venv', 'bin', exeName('python')),
        path.join(process.resourcesPath, 'backend', exeName('python')),
        'C:\\Users\\User\\Python312\\python.exe',
    ];
    SERVER_DIR = path.join(process.resourcesPath, 'backend', 'server');
}

// Tailored runtime environment for the packaged build: learned data + media go
// to the user profile (seeded from the bundled defaults on first run) so a
// read-only install never loses training.
function buildServerEnv() {
    const env = { ...process.env };
    if (app.isPackaged) {
        env.GALAXYPRON_DATA_DIR = path.join(app.getPath('userData'), 'data');
        env.GALAXYPRON_GEN_DIR = path.join(app.getPath('userData'), 'generated');
        env.GALAXYPRON_SEED = path.join(SERVER_DIR, 'data');
        if (PUBLIC_BUILD) {
            // Public builds: trainer/admin endpoints always locked server-side,
            // and continuous learning never autostarts (it isn't part of the
            // shipped UI anyway).
            env.GALAXYPRON_PUBLIC = '1';
            env.GALAXYPRON_NO_AUTOSTART = '1';
        } else {
            env.GALAXYPRON_ADMIN_KEY = ADMIN_KEY;
        }
        if (awaitVerify()) {
            // Training must NOT start before the verification unlocks the app:
            // the shell starts it explicitly once the disc is verified.
            env.GALAXYPRON_NO_AUTOSTART = '1';
        }
    }
    return env;
}

let serverProc = null;
let mainWindow = null;
let quitting = false;
let serverRestarting = false;

// Find a usable python
function findPython() {
    for (const p of PYTHON_CANDIDATES) {
        if (fs.existsSync(p)) return p;
    }
    return null;
}

function startServer() {
    const python = findPython();
    if (!python) {
        console.error('Python not found. Install Python 3.9+ (we look for venv or C:\\Users\\User\\Python312)');
        return;
    }
    const serverPy = path.join(SERVER_DIR, 'app.py');
    serverProc = spawn(python, [serverPy], { cwd: SERVER_DIR, env: buildServerEnv(), stdio: 'inherit' });
    serverProc.on('exit', (code) => {
        serverProc = null;
        console.log('Server process exited:', code);
        // Auto-restart the backend if it dies unexpectedly so the app never
        // goes dark. Back off on repeated crashes to avoid a hot loop.
        if (!quitting) {
            if (serverRestarting) return;
            serverRestarting = true;
            setTimeout(() => {
                serverRestarting = false;
                if (!quitting && !serverProc) {
                    console.log('Restarting backend server...');
                    startServer();
                }
            }, 1500);
        }
    });
}


function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1200,
        height: 820,
        minWidth: 720,
        minHeight: 540,
        title: 'galaxypron',
        icon: path.join(__dirname, 'icon.png'),
        backgroundColor: '#0b0e1a',
        show: false,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            nodeIntegration: false,
            contextIsolation: true,
        },
    });

    mainWindow.setMenuBarVisibility(false);
    mainWindow.on('closed', () => {
        mainWindow = null;
    });

    // Start with the copy-protection gate (packaged) or the loader (dev).
    if (app.isPackaged) loadScreen('verify', 'Checking disks…');
    else loadScreen('spinner');

    mainWindow.once('ready-to-show', () => mainWindow.show());
}

function screenHTML(kind, status) {
    const base = `<div style="font-size:44px;margin-bottom:16px">`;
    if (kind === 'verify') {
        return `<!doctype html><html style="background:#0b0e1a">
            <body style="margin:0;height:100vh;display:flex;flex-direction:column;
              align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;
              color:#eceef6;background:#0b0e1a;padding:24px;text-align:center">
              ${base}\u{1F512}</div>
              <div style="font-weight:600;font-size:20px">Input verification disk to continue</div>
              <div style="color:#8e8ea0;font-size:13px;margin-top:10px;max-width:520px">
                Insert your galaxypron verification DVD-RW containing
                <b style="color:#c7c9d6">galaxypron.verify</b>.
                The template is at <b style="color:#c7c9d6">C:\Users\&lt;you&gt;\Desktop\galaxypron.verify</b>
                — burn it onto a blank DVD-RW once.
              </div>
              <div id="st" style="color:#34d399;font-size:13px;margin-top:18px">${status || ''}</div>
              <div style="margin-top:26px;color:#566;font-size:12px">App stays locked until the disc is verified.</div>
            </body></html>`;
    }
    if (kind === 'saving') {
        return `<!doctype html><html style="background:#0b0e1a">
            <body style="margin:0;height:100vh;display:flex;flex-direction:column;
              align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;
              color:#eceef6;background:#0b0e1a;padding:24px;text-align:center">
              ${base}\u{1F4BE}</div>
              <div style="font-weight:600;font-size:18px">Verification disk removed</div>
              <div style="color:#8e8ea0;font-size:13px;margin-top:10px">
                Saving all progress and closing galaxypron…
              </div>
            </body></html>`;
    }
    return `<!doctype html><html style="background:#0b0e1a">
        <body style="margin:0;height:100vh;display:flex;flex-direction:column;
          align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;
          color:#eceef6;background:#0b0e1a">
          ${base}\u{1F4A7}</div>
          <div style="font-weight:600;font-size:18px">galaxypron is waking up…</div>
          <div style="color:#8e8ea0;font-size:13px;margin-top:8px">
            Loading the language brain (takes a minute on a DVD install)</div>
          <div style="margin-top:22px;width:180px;height:4px;border-radius:2px;
            background:rgba(255,255,255,.12);overflow:hidden">
            <div id="bar" style="height:100%;width:0%;background:#34d399;transition:width .4s"></div>
          </div>
          <script>
            let w=0;
            const bar=document.getElementById('bar');
            setInterval(()=>{ w=w>=92?92:w+3; bar.style.width=w+'%'; }, 600);
          </script>
        </body></html>`;
}

function loadScreen(kind, status) {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(screenHTML(kind, status)));
}

// Returns true once the backend HTTP server answers on /api/status.
function serverIsUp() {
    return new Promise((resolve) => {
        const req = http.get({ host: '127.0.0.1', port: PORT, path: '/api/status', timeout: 2000 }, (res) => {
            res.resume();
            resolve(true);
        });
        req.on('timeout', () => { req.destroy(); resolve(false); });
        req.on('error', () => resolve(false));
    });
}

// Poll until the backend is reachable (first launch can be slow: the portable
// exe extracts ~3.4GB and torch loads ~1GB of models), then load the UI.
async function pollForServer(timeoutMs = 600000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs && !quitting) {
        if (await serverIsUp()) return true;
        await new Promise((r) => setTimeout(r, 750));
        if (mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents.isLoadingMainFrame()) {
            try { mainWindow.webContents.stop(); } catch (e) { /* ignore */ }
        }
    }
    return false;
}

// ---- Copy protection -------------------------------------------------------

// Drive letters of removable (2) and CD/DVD (5) drives. Runs once; the DVD
// writer's letter is stable whether or not a disc is currently inside.
function getCandidateDrives() {
    try {
        const out = require('child_process').execFileSync(
            'powershell.exe',
            ['-NoProfile', '-NonInteractive', '-Command',
                'Get-CimInstance Win32_LogicalDisk | ForEach-Object { if ($_.DriveType -eq 2 -or $_.DriveType -eq 5) { $_.DeviceID } }'],
            { encoding: 'utf8', timeout: 15000 });
        return out.split(/\r?\n/).map((s) => s.trim()).filter((s) => /^[A-Z]:$/.test(s));
    } catch (e) {
        return [];
    }
}

function driveHasVerify(drive) {
    try {
        const f = drive + '\\' + VERIFY_FILENAME;
        if (!fs.existsSync(f)) return false;
        return fs.readFileSync(f, 'utf8').trim() === VERIFY_TOKEN;
    } catch (e) {
        return false;
    }
}

// Whether copy-protection actually engages on this machine: it needs a packaged
// Windows build AND at least one optical drive present (DVD verification needs
// an optical reader — machines without one run freely).
function hasOpticalDrive() {
    if (!IS_WIN) return false;
    try {
        const out = require('child_process').execFileSync(
            'powershell.exe',
            ['-NoProfile', '-NonInteractive', '-Command',
                '(Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 5 } | Measure-Object).Count'],
            { encoding: 'utf8', timeout: 15000 });
        return parseInt(String(out).trim(), 10) > 0;
    } catch (e) {
        return false;
    }
}

function awaitVerify() {
    return VERIFY_ARMED && hasOpticalDrive();
}

// Headers for backend admin calls. The packaged shell signs with the admin key
// so the training-control endpoints accept its internal commands; the web UI
// never carries this key, so users can't tune training from a browser. Public
// builds don't sign anything — their endpoints are 403'd server-side anyway.
function adminHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (app.isPackaged && !PUBLIC_BUILD) h['X-Admin-Key'] = ADMIN_KEY;
    return h;
}

// Polls the DVD drive(s) until a valid key is found.
// Resolves to the verifying drive letter (e.g. 'D:'), or null on quit.
async function pollForVerification() {
    const drives = getCandidateDrives();
    let lastMsg = '';
    while (!quitting) {
        for (const d of drives) {
            if (quitting) break;
            if (driveHasVerify(d)) {
                loadScreen('spinner');
                return d;
            }
        }
        const m = 'Waiting for verification disk…';
        if (m !== lastMsg) { lastMsg = m; loadScreen('verify', m); }
        await new Promise((r) => setTimeout(r, 1000));
    }
    return null;
}

// Copy-protection state. The app runs freely while a valid verification disc
// is present. The instant it is removed the app LOCKS back to the verification
// screen (it does not quit, so work is never lost) and resumes as soon as the
// disc is re-inserted.
let locked = false;

function appIsVerified() {
    for (const d of getCandidateDrives()) {
        if (driveHasVerify(d)) return d;
    }
    return null;
}

function trainerCommand(action) {
    return fetch(`http://127.0.0.1:${PORT}/api/system/trainer`, {
        method: 'POST',
        headers: adminHeaders(),
        body: JSON.stringify({ action }),
    }).catch(() => { /* backend may be mid-restart */ });
}

// Lock: pause learning and show the gate while the disc is out. The backend
// stays up and the window stays open, so nothing is lost and unlock is instant.
function lockApp() {
    if (locked || quitting) return;
    locked = true;
    if (mainWindow && !mainWindow.isDestroyed()) {
        loadScreen('verify', 'Disc removed — galaxypron is locked until it is re-inserted.');
    }
    trainerCommand('stop');
}

// Unlock: reload the UI and resume continuous training once the disc is back.
function unlockApp() {
    if (!locked || quitting) return;
    locked = false;
    loadScreen('spinner');
    (async () => {
        const up = await pollForServer();
        if (up && mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.loadURL(`http://127.0.0.1:${PORT}/`);
        }
        trainerCommand('start');
    })();
}

// Poll the removable/optical drives for a valid verification file. Detects
// ejection (the file stops being readable even though the drive letter stays)
// AND re-insertion, without ever quitting the app.
function ejectMonitorTick() {
    if (appIsVerified()) unlockApp();
    else lockApp();
}

let ejectTimer = null;
function startEjectMonitor() {
    stopEjectMonitor();
    ejectTimer = setInterval(ejectMonitorTick, 1500);
    ejectMonitorTick();
}

function stopEjectMonitor() {
    if (ejectTimer) { clearInterval(ejectTimer); ejectTimer = null; }
}

let verifiedSource = null;

// ---- Persistent disc watcher ---------------------------------------------
// Installing a scheduled task + spawning a headless watcher means that when
// the verification disc is inserted, the app auto-launches even if it is
// closed. The watcher must live at a stable path: the portable exe extracts
// itself to a temp dir that is deleted on quit, so it cannot point into
// resources. The app copies bundled disk_watcher.py into the user's Python
// folder on every start to keep that copy fresh.
const WATCHER_TASK = 'galaxypron-disk-watcher';
const WATCHER_PY = 'C:\\Users\\User\\Python312\\pythonw.exe';
const WATCHER_DIR = path.dirname(WATCHER_PY);
const STABLE_WATCHER = path.join(WATCHER_DIR, 'galaxypron_disk_watcher.py');
const WATCHER_WRAPPER = path.join(WATCHER_DIR, 'galaxypron_watcher_run.cmd');
function startupFolder() {
    return path.join(process.env.APPDATA || '',
        'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup');
}
const WATCHER_STARTUP_VBS = path.join(startupFolder(), 'galaxypron_watcher.vbs');

function ensureDiskWatcher() {
    if (!app.isPackaged) return;
    const bundledWatcher = path.join(process.resourcesPath, 'backend', 'disk_watcher.py');
    const target = process.execPath;
    // 1) Keep the watcher script fresh at its stable location.
    try {
        if (fs.existsSync(bundledWatcher)) {
            fs.copyFileSync(bundledWatcher, STABLE_WATCHER);
        }
    } catch (e) { /* ignore */ }
    // 2) Startup-folder .vbs: runs at every logon without admin rights and
    //    launches pythonw headlessly (0 = hidden window, False = no wait).
    try {
        const cmd = ['"' + WATCHER_PY + '"', '"' + STABLE_WATCHER + '"',
            '--target', '"' + target + '"'].join(' ');
        fs.writeFileSync(WATCHER_STARTUP_VBS,
            'Set sh = CreateObject("WScript.Shell")\r\n' +
            'sh.Run "' + cmd + '", 0, False\r\n');
    } catch (e) { /* ignore */ }
    // 3) Optional: also refresh a logon scheduled task where policy allows it.
    try {
        fs.writeFileSync(WATCHER_WRAPPER,
            `@echo off\r\n"${WATCHER_PY}" "${STABLE_WATCHER}" --target "${target}"\r\n`);
        execFileSync('schtasks.exe', ['/Create', '/F',
            '/TN', WATCHER_TASK,
            '/TR', `"${WATCHER_WRAPPER}"`,
            '/SC', 'ONLOGON', '/RL', 'LIMITED'],
            { timeout: 20000 });
    } catch (e) { /* task creation denied by policy — startup entry still works */ }
    // 4) And make sure a watcher instance is alive right now.
    try {
        const w = spawn(WATCHER_PY, [STABLE_WATCHER, '--target', target],
            { detached: true, stdio: 'ignore' });
        w.unref();
    } catch (e) { /* ignore */ }
}

app.whenReady().then(async () => {
    startServer();
    createWindow();

    if (awaitVerify()) {
        ensureDiskWatcher();
        verifiedSource = await pollForVerification();
        if (!verifiedSource) return; // never verified — window stays locked
        startEjectMonitor();
    } else {
        loadScreen('spinner');
    }

    const up = await pollForServer();
    if (up && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.loadURL(`http://127.0.0.1:${PORT}/`);
    }

    // Auto-start the 24/7 continuous trainer on verified installs (the backend
    // autostarts it on open builds because NO_AUTOSTART is only set for them).
    if (awaitVerify()) {
        try {
            await fetch(`http://127.0.0.1:${PORT}/api/system/trainer`, {
                method: 'POST',
                headers: adminHeaders(),
                body: JSON.stringify({ action: 'start' }),
            });
        } catch (e) { /* ignore */ }
    }

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
});

// Clean up the server when the app closes
app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        quitting = true;
        if (serverProc) serverProc.kill();
        app.quit();
    }
});

app.on('before-quit', () => {
    quitting = true;
    stopEjectMonitor();
    if (serverProc) {
        try { serverProc.kill(); } catch (e) { /* ignore */ }
    }
});
