import os
import sys
import json
import time
import atexit
import threading
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_from_directory, make_response
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from flask.json.provider import DefaultJSONProvider
import math
from agent_core import AIAgent
from learning_engine import LearningEngine
from training_engine import TrainingEngine
from continuous_trainer import ContinuousTrainer
from media_gen import MediaGenerator
from storage_tier import TierStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', template_folder='templates')

# Safe JSON: browsers' JSON.parse rejects the literal NaN/Infinity tokens that
# json.dumps emits for non-finite floats (e.g. torch loss_history). Always emit
# null instead so the web UI never chokes when the trainer reports a NaN loss.
def _json_sanitize(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _json_sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_sanitize(v) for v in o]
    return o

class _SafeJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return super().dumps(_json_sanitize(obj), **kwargs)

app.json = _SafeJSONProvider(app)

CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

DATA_DIR = Path(__file__).parent / 'data'
# Packaged single-file app: persist learned data + generated media in the
# user profile (the bundled copy inside the exe is read-only and re-extracted
# every launch, so writing there would lose training). First run copies the
# bundled defaults next to the exe-equivalent so knowledge survives restarts.
_BUNDLED_DATA = DATA_DIR
DATA_DIR = Path(os.environ.get('GALAXYPRON_DATA_DIR') or (DATA_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Single-process guard: only ONE galaxypron server may run per machine, no
# matter how many wrappers (script, scheduled task, packaged shell) try. A
# second instance exits before it can touch the training files or steal the
# HTTP port, preventing the dual-writer race on knowledge.json / text_model.pt.
def _pid_alive(pid):
    if os.name == 'nt':
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            status = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(status))
            return status.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_TEMP_DIR = Path(os.environ.get('TEMP') or os.environ.get('TMP') or '/tmp')
SERVER_LOCK = _TEMP_DIR / 'galaxypron_server.lock'
_OWN_LOCK = False


def _acquire_server_lock():
    global _OWN_LOCK
    try:
        if SERVER_LOCK.exists():
            try:
                _pid = int(SERVER_LOCK.read_text().strip())
            except ValueError:
                _pid = None
            if _pid and _pid != os.getpid() and _pid_alive(_pid):
                return False
        SERVER_LOCK.write_text(str(os.getpid()))
        _OWN_LOCK = True
        atexit.register(lambda: SERVER_LOCK.unlink(missing_ok=True)
                        if _OWN_LOCK else None)
        return True
    except Exception:
        return True


if not _acquire_server_lock():
    logger.warning('Another galaxypron server instance is running (lock %s); exiting.',
                   SERVER_LOCK)
    sys.exit(0)

GEN_DIR = Path(__file__).parent / 'generated'
GEN_DIR = Path(os.environ.get('GALAXYPRON_GEN_DIR') or (GEN_DIR))
GEN_DIR.mkdir(parents=True, exist_ok=True)

_GALAXYPRON_SEED = os.environ.get('GALAXYPRON_SEED')
if _GALAXYPRON_SEED:
    _SEED = Path(_GALAXYPRON_SEED)
    if _SEED.is_dir() and _SEED != DATA_DIR:
        for _name in os.listdir(_SEED):
            _src = _SEED / _name
            _dst = DATA_DIR / _name
            if _src.is_file() and not _dst.exists():
                try:
                    import shutil
                    shutil.copy2(_src, _dst)
                    logger.info(f'[seed] copied {_name} into persistent data dir')
                except Exception:
                    pass

learning_engine = LearningEngine(DATA_DIR)
training_engine = TrainingEngine(DATA_DIR)

# Tiered storage: the SSD cache dir above is the write target, the external
# HDD gets a background mirror so nothing is lost if the SSD drive fills up.
# Set GALAXYPRON_HDD_DIR to point at the archive drive/folder (default E:).
TIER_ARTIFACTS = ('text_model.pt', 'text_vocab.json', 'knowledge.json')
storage_tier = TierStore(DATA_DIR, os.environ.get('GALAXYPRON_HDD_DIR') or r'E:\galaxypron-data')
if storage_tier.enabled:
    storage_tier.mirror_all_now(TIER_ARTIFACTS)


def _tier_sync_loop():
    while True:
        time.sleep(300)
        try:
            storage_tier.mirror_all_now(TIER_ARTIFACTS)
        except Exception:
            pass


if storage_tier.enabled:
    threading.Thread(target=_tier_sync_loop, daemon=True).start()

continuous_trainer = ContinuousTrainer(DATA_DIR, tier=storage_tier if storage_tier.enabled else None)
media_generator = MediaGenerator(GEN_DIR)
agent = AIAgent(DATA_DIR, learning_engine, training_engine, media_generator=media_generator)

# ---------------------------------------------------------------------------
# Admin gate: the public app hides every training-control surface from the UI.
# When GALAXYPRON_ADMIN_KEY is set, the tuning/admin endpoints below require an
# X-Admin-Key header that the desktop shell supplies internally, so web users
# can't fiddle with training files / model settings. If the env var is unset
# (plain `python app.py`), the endpoints stay open for local development.
ADMIN_KEY = os.environ.get('GALAXYPRON_ADMIN_KEY', '')

# Public (release) builds lock training down completely: the desktop shell sets
# GALAXYPRON_PUBLIC=1 and the stripped UI never exposes these controls, and even
# a direct API call can't touch the trainer / model settings in this mode.
PUBLIC_MODE = os.environ.get('GALAXYPRON_PUBLIC', '') == '1'

def _require_admin():
    if PUBLIC_MODE:
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403
    # The desktop window is a loopback client: it must be able to start/stop
    # training and tune the model in the full build, so allow it without a key.
    if not ADMIN_KEY or request.remote_addr in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return None
    if request.headers.get('X-Admin-Key', '') == ADMIN_KEY:
        return None
    return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

# Background training: debounce so we batch-visits and avoid blocking requests
_training_lock = threading.Lock()
_pending_train = False
_last_train_trigger = 0.0


def _scheduled_train():
    global _pending_train
    with _training_lock:
        if not _pending_train:
            return
        _pending_train = False
        visits = learning_engine.data.get('visits', [])
    try:
        result = training_engine.train(visits[-300:], epochs=6)
        if result.get('trained'):
            logger.info(
                f"[training] epochs={result['epochs']} examples={result['examples']} "
                f"loss={result['avg_loss']} device={result['device']}"
            )
    except Exception as exc:
        logger.warning(f"[training] failed: {exc}")


def _request_training():
    global _pending_train, _last_train_trigger
    with _training_lock:
        _pending_train = True
        _last_train_trigger = time.time()
    t = threading.Thread(target=_scheduled_train, daemon=True)
    t.start()

@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/api/status')
def status():
    return jsonify({
        'status': 'online',
        'agent': agent.get_status(),
        'learning': learning_engine.get_stats(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    message = data.get('message', '')
    context = data.get('context', {})
    
    response = agent.process_message(message, context)
    
    learning_engine.log_interaction({
        'user_message': message,
        'agent_response': response['text'],
        'timestamp': datetime.now().isoformat(),
        'context': context
    })
    
    return jsonify(response)

@app.route('/api/learning/data', methods=['GET'])
def get_learning_data():
    return jsonify(learning_engine.get_learning_summary())

@app.route('/api/learning/patterns', methods=['GET'])
def get_patterns():
    return jsonify(learning_engine.get_patterns())

@app.route('/api/report/weekly')
def weekly_report():
    """Weekly 'what I learned about you' recap: last-7-days browsing + trainer."""
    report = learning_engine.weekly_report()
    try:
        t = continuous_trainer.status()
        kb = t.get('knowledge') or {}
        tm = t.get('text_model') or {}
        loss = None
        lh = tm.get('loss_history') or []
        if lh:
            loss = lh[-1]
        report['trainer'] = {
            'running': bool(t.get('running')),
            'device': (t.get('device_effective') or t.get('device') or 'cpu'),
            'started_at': t.get('started_at'),
            'articles_ingested': kb.get('articles_ingested', 0),
            'vocabulary_size': kb.get('vocabulary_size', 0),
            'words_trained': tm.get('words_trained', 0),
            'epochs': tm.get('epochs', 0),
            'last_loss': loss,
            'top_words': [w.get('word') for w in (kb.get('most_common_words') or [])[:8]],
        }
    except Exception:
        report['trainer'] = {}
    return jsonify(report)

@app.route('/mobile')
def mobile_view():
    """Phone/LAN-friendly stats page (no sidebar; stacked, touch-first)."""
    resp = make_response(render_template('mobile.html'))
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/api/history', methods=['GET'])
def get_history():
    limit = request.args.get('limit', 50, type=int)
    return jsonify(agent.get_history(limit))

@app.route('/api/browser-event', methods=['POST'])
def browser_event():
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data'}), 400
    learning_engine.process_browser_data(data)
    agent.update_context(data)
    _request_training()
    return jsonify({'status': 'ok'})

@app.route('/api/training', methods=['GET'])
def get_training():
    stats = training_engine.stats()
    try:
        stats['text_model'] = continuous_trainer.text_model.progress()
        mi = continuous_trainer.text_model.mode_info()
        # report the device the 24/7 Wikipedia trainer actually uses, so the
        # dashboard stat follows the user's CPU/GPU selection instead of only
        # the tiny browsing-classifier engine
        stats['device'] = mi['device']
        stats['device_effective'] = mi['effective']
        stats['device_note'] = mi['note']
        stats['mode'] = mi['selected']
    except Exception:
        stats['text_model'] = None
    return jsonify(stats)

@app.route('/api/training', methods=['POST'])
def retrain():
    deny = _require_admin()
    if deny:
        return deny
    visits = learning_engine.data.get('visits', [])
    result = training_engine.train(visits[-500:], epochs=int(request.json.get('epochs', 10)) if request.json else 10)
    return jsonify(result)

@app.route('/api/system/mode', methods=['GET'])
def get_mode():
    deny = _require_admin()
    if deny:
        return deny
    return jsonify(training_engine.mode_info())

@app.route('/api/system/mode', methods=['POST'])
def set_mode():
    deny = _require_admin()
    if deny:
        return deny
    mode = (request.json or {}).get('mode', 'gpu')
    result = training_engine.set_mode(mode)
    try:
        ti = continuous_trainer.set_train_mode(result['selected'])
        # surface the 24/7 trainer's effective device to the UI alongside the
        # browsing-classifier info so both reflect the same real device
        result['device'] = ti['device']
        result['effective'] = ti['effective']
        result['trainer_note'] = ti['note']
    except Exception:
        pass
    return jsonify(result)

@app.route('/api/system/trainer', methods=['GET'])
def trainer_status():
    return jsonify(continuous_trainer.status())

@app.route('/api/debug/state', methods=['GET'])
def debug_state():
    """Diagnostics for 'nothing is showing / not training' reports."""
    try:
        st = continuous_trainer.status()
    except Exception:
        st = {}
    kb = st.get('knowledge') or {}
    tm = st.get('text_model') or {}
    artifacts = {}
    for n in TIER_ARTIFACTS:
        row = {'ssd': None, 'hdd': None}
        sp = storage_tier.cache(n)
        if sp.exists():
            row['ssd'] = [sp.stat().st_size, sp.stat().st_mtime]
        if storage_tier.enabled:
            hp = storage_tier.hdd / n
            if hp.exists():
                row['hdd'] = [hp.stat().st_size, hp.stat().st_mtime]
        artifacts[n] = row
    return jsonify({
        'pid': os.getpid(),
        'python': sys.executable,
        'lock_owner': _OWN_LOCK,
        'lock_file': str(SERVER_LOCK),
        'data_dir': str(DATA_DIR),
        'port': 5000,
        'tier_enabled': storage_tier.enabled,
        'hdd_dir': str(storage_tier.hdd) if storage_tier.enabled else None,
        'trainer_running': bool(st.get('running')),
        'started_at': st.get('started_at'),
        'last_error': st.get('last_error'),
        'articles_ingested': kb.get('articles_ingested', 0),
        'vocabulary_size': kb.get('vocabulary_size', 0),
        'total_words': kb.get('total_words', 0),
        'text_examples_seen': tm.get('examples_seen', 0),
        'text_words_trained': tm.get('words_trained', 0),
        'text_vocab_size': tm.get('vocab_size', 0),
        'artifacts': artifacts,
    })

@app.route('/api/system/train-control', methods=['GET'])
def get_train_control():
    deny = _require_admin()
    if deny:
        return deny
    return jsonify(continuous_trainer.controls())

@app.route('/api/system/train-control', methods=['POST'])
def set_train_control():
    deny = _require_admin()
    if deny:
        return deny
    data = request.json or {}
    result = continuous_trainer.set_controls(**data)
    # keep the neural-network engine's device mode in sync with the choice
    mode = data.get('mode')
    if mode:
        training_engine.set_mode(mode)
    return jsonify(result)

@app.route('/api/system/trainer', methods=['POST'])
def trainer_control():
    deny = _require_admin()
    if deny:
        return deny
    action = (request.json or {}).get('action', 'start')
    if action == 'start':
        result = continuous_trainer.start()
    else:
        result = continuous_trainer.stop()
    return jsonify(result)

@app.route('/api/system/shutdown', methods=['POST'])
def graceful_shutdown():
    """Stop training, force a synchronous checkpoint, then exit. Used by the
    desktop shell when the verification disk is ejected mid-run."""
    deny = _require_admin()
    if deny:
        return deny
    saved = False
    try:
        continuous_trainer.stop()
        continuous_trainer.kb.flush()
        try:
            continuous_trainer._save_sweep_state()
        except Exception:
            pass
        saved = True
    except Exception:
        pass

    def _exit():
        try:
            socketio.stop()
        except Exception:
            pass
        os._exit(0)

    threading.Timer(1.5, _exit).start()
    return jsonify({'ok': True, 'saved': saved})

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(agent.get_config())

@app.route('/api/config', methods=['POST'])
def update_config():
    data = request.json
    agent.update_config(data)
    return jsonify({'status': 'updated'})

@app.route('/generated/<path:filename>')
def generated_media(filename):
    return send_from_directory(GEN_DIR, filename)

@app.route('/api/generate', methods=['POST'])
def generate_media():
    data = request.json or {}
    kind = data.get('kind', 'image')
    prompt = data.get('prompt', '')
    if kind == 'video':
        result = media_generator.generate_video(prompt=prompt)
    else:
        result = media_generator.generate_image(prompt=prompt)
    result['url'] = '/generated/' + result['file']
    return jsonify(result)

@socketio.on('connect')
def handle_connect():
    logger.info('Client connected')
    emit('status', {'status': 'connected'})

@socketio.on('browser_data')
def handle_browser_data(data):
    logger.info(f'Received browser data: {data.get("type", "unknown")}')
    learning_engine.process_browser_data(data)
    agent.update_context(data)
    _request_training()
    emit('data_processed', {'status': 'ok'})

@socketio.on('user_message')
def handle_user_message(data):
    message = data.get('message', '')
    response = agent.process_message(message, data.get('context', {}))
    emit('agent_response', response)

def create_directories():
    for subdir in ['templates', 'static', 'static/css', 'static/js', 'data']:
        (Path(__file__).parent / subdir).mkdir(exist_ok=True)

if __name__ == '__main__':
    create_directories()
    logger.info('Starting galaxypron Edge AI Agent server...')
    # 24/7 learning starts automatically with the server and runs regardless
    # of the GUI, so training never stops. Set GALAXYPRON_NO_AUTOSTART=1 to
    # keep the trainer off until the user starts it from the UI.
    if os.environ.get('GALAXYPRON_NO_AUTOSTART', '') != '1' and not PUBLIC_MODE:
        continuous_trainer.start()
        logger.info('24/7 continuous Wikipedia training started automatically.')
    # Debug/reloader is off by default for a robust single-process desktop
    # launch; set GALAXYPRON_DEBUG=1 to enable the Flask auto-reloader.
    debug = os.environ.get('GALAXYPRON_DEBUG', '') == '1'
    socketio.run(app, host='0.0.0.0', port=5000, debug=debug, allow_unsafe_werkzeug=True)
