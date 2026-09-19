/*
 * prepare-backend.js
 * Syncs the Python backend into desktop/portable-staging/backend/server so
 * electron-builder can ship it as extraResources. Run BEFORE building:
 *
 *   node prepare-backend.js            # full version (trainer/dashboard kept)
 *   node prepare-backend.js --public   # public release (trainer UI stripped)
 *
 * The public build overlays desktop/public-ui (UI without the Dashboard /
 * training controls) and drops a `.public` marker that the desktop shell uses
 * to disable copy-protection and lock the training-control endpoints.
 *
 * The per-OS Python runtime is bundled separately:
 *   - Windows: a venv created on Windows (torch + deps installed into it)
 *   - macOS / Linux: create a venv on the target OS and
 *       pip install -r backend/requirements.txt
 *   The desktop shell (main.js) resolves venv per platform.
 */
const fs = require('fs');
const path = require('path');

const PUBLIC = process.argv.includes('--public');

const BACKEND_SRC = path.resolve(__dirname, '..', 'server');
const OUT_DIR = path.resolve(__dirname, 'portable-staging', 'backend', 'server');
const REQUIREMENTS_SRC = path.resolve(__dirname, '..', 'requirements.txt');
const REQUIREMENTS_OUT = path.resolve(__dirname, 'portable-staging', 'backend', 'requirements.txt');
const PUBLIC_MARKER = path.resolve(__dirname, 'portable-staging', 'backend', '.public');
const PUBLIC_UI = path.resolve(__dirname, 'public-ui');

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      // skip caches / runtime noise
      if (entry.name === '__pycache__' || entry.name.startsWith('.')) continue;
      copyDir(s, d);
    } else {
      fs.copyFileSync(s, d);
    }
  }
}

fs.mkdirSync(OUT_DIR, { recursive: true });
copyDir(BACKEND_SRC, OUT_DIR);
if (fs.existsSync(REQUIREMENTS_SRC)) {
  fs.copyFileSync(REQUIREMENTS_SRC, REQUIREMENTS_OUT);
}

if (PUBLIC) {
  // Overlay the stripped UI over the backend so the public app has no
  // trainer/dashboard surface at all.
  for (const rel of ['templates/index.html', 'static/js/main.js']) {
    const s = path.join(PUBLIC_UI, rel);
    if (!fs.existsSync(s)) {
      console.error(`public-ui/${rel} missing — public build aborted`);
      process.exit(1);
    }
    const d = path.join(OUT_DIR, rel);
    fs.mkdirSync(path.dirname(d), { recursive: true });
    fs.copyFileSync(s, d);
  }
  fs.mkdirSync(path.dirname(PUBLIC_MARKER), { recursive: true });
  fs.writeFileSync(PUBLIC_MARKER, 'galaxypron public build\n');
  console.log('PUBLIC build: trainer UI stripped + .public marker written');
} else {
  // Remove any stale marker so a full build never carries stripped UI.
  if (fs.existsSync(PUBLIC_MARKER)) fs.unlinkSync(PUBLIC_MARKER);
  console.log('Full build: trainer/dashboard UI included');
}

// make sure agent/training assets the front-end references exist
for (const rel of ['templates', 'static']) {
  if (!fs.existsSync(path.join(OUT_DIR, rel))) {
    console.warn(`WARNING: server/${rel} not found — is the backend source complete?`);
  }
}

console.log('Backend synced to', path.relative(__dirname, OUT_DIR));