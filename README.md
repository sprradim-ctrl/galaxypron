# galaxypron

An Edge AI Agent that learns from how you browse Microsoft Edge, chats about your
habits, trains a private local language model on Wikipedia, and answers questions
it didn't already know — live — from the web.

- **Private & local** — everything (browsing data, the trained model) lives on
  your own machine.
- **24/7 self-training** — a continuous trainer quietly learns from Wikipedia in
  the background between your visits.
- **Living answers** — if the agent doesn't know the answer, it researches the
  question on the web and composes a fresh, source-based reply.
- **Weekly Brain Report** — a weekly summary of what it learned about you
  (top sites, interests, busiest days, model activity).
- **Mobile stats** — open the same status page on your phone from the same Wi-Fi:
  `http://<your-pc-ip>:5000/mobile`.

## Two builds

| Build | Training dashboard | Training settings | Copy protection | Training API |
|-------|-------------------|-------------------|-----------------|--------------|
| **Full** (private/publisher) | Yes | Yes | Optional (Windows, disc) | Open (admin key) |
| **Public** (released) | Hidden | Hidden | Disabled | Locked (403) |

The **full** build keeps the trainer, dashboard and training controls. The
**public** build ships the same app with the training surface stripped out and
the training endpoints locked server-side, so consumers can use it freely but
can't change the model/training files.

## Build from source

Requirements per target OS:

- Node.js 18+ and npm
- Python 3.9+
- Additional files (not committed): a Python venv containing the backend deps
  and a `server/data` folder with the bundled defaults.

```bash
# 1) Backend Python environment (on the OS you're building for)
python -m venv venv
venv\Scripts\pip install -r requirements.txt        # Windows
venv/bin/pip install -r requirements.txt            # macOS / Linux

# torch installs CPU wheels by default on all platforms (DirectML is a Windows
# optional extra and is auto-skipped elsewhere).

# 2) Stage the backend + build
cd desktop
node prepare-backend.js          # full version
# node prepare-backend.js --public   # public release (strips trainer UI)

npm install
npm run dist:win                 # Windows portable exe
npm run dist:mac                 # macOS dmg + zip   (build on a Mac)
npm run dist:linux               # Linux AppImage + deb
```

Outputs land in `desktop/dist` (full) or `desktop/dist-public` (public builds).

## Python requirements

`requirements.txt` — Flask, Flask-SocketIO, flask-cors, requests, numpy,
Pillow, torch, torchvision (CPU).

## Data & privacy

- Learned data and generated media are stored per-user; packaged builds write to
  the OS user-data folder (never inside the app install).
- `server/data` is machine-local (the trained model + your browsing history) and
  is excluded from source control.

## Copy protection (full builds only)

Full builds can optionally lock the app until a verification file
(`galaxypron.verify`) is present on an optical/removable drive. It only arms on
Windows machines that actually have an optical drive and is skipped entirely on
public releases and non-Windows platforms.

## License

MIT — see [LICENSE](LICENSE).