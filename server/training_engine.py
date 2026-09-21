import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim

# ---------------------------------------------------------------------------
# DirectML backend: lets PyTorch train on ANY DirectX 12 GPU (AMD, Intel,
# NVIDIA) on Windows via Microsoft's torch-directml plugin. This is how the
# AMD Radeon RX 6400 gets real GPU acceleration (PyTorch CUDA is NVIDIA-only;
# ROCm is Linux-only).
_TORCH_DML_AVAILABLE = False
_DML_DEVICE = None
_DML_NAME = ''
try:
    import torch_directml
    _DML_DEVICE = torch_directml.device()
    _DML_NAME = str(torch_directml.device_name(0)).strip()
    _TORCH_DML_AVAILABLE = True
except Exception:
    torch_directml = None


def _is_dml_device(dev):
    try:
        return dev is not None and str(dev).startswith('privateuseone')
    except Exception:
        return False


def _device_label(dev):
    """Friendly label ('cpu' / 'cuda' / 'dml') for a torch device object."""
    if _is_dml_device(dev):
        return 'dml'
    try:
        return dev.type if dev is not None else 'cpu'
    except Exception:
        return 'cpu'

# Consistent featured category ordering used as the classification label space.
CATEGORIES = [
    'tech', 'social', 'entertainment', 'news', 'shopping',
    'education', 'finance', 'general'
]

MODEL_PATH = 'model.pt'
META_PATH = 'model_meta.json'
TEXT_MODEL_PATH = 'text_model.pt'
TEXT_VOCAB_PATH = 'text_vocab.json'


class BrowsingNet(nn.Module):
    """Small feedforward network that learns from browsing behaviour.

    Input features (per visit):
        0: hour-of-day normalized to [-1, 1]  (time-of-day rhythm)
        1: day-of-week normalized to [-1, 1]  (weekly rhythm)
        2: log-scaled visit intensity for the domain
        3: minutes since this domain was last visited (decayed)

    Outputs:
        logits over CATEGORIES (which kind of site the user visits)
        a scalar 'engagement' head (how strongly the user returns)
    """

    def __init__(self, n_features=4, hidden=32, n_categories=len(CATEGORIES)):
        super().__init__()
        self.shared = nn.Sequential(
            nn.Linear(n_features, hidden),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
        )
        self.classifier = nn.Linear(hidden, n_categories)
        self.engagement = nn.Linear(hidden, 1)

    def forward(self, x):
        h = self.shared(x)
        cls_logits = self.classifier(h)
        eng = torch.sigmoid(self.engagement(h))
        return cls_logits, eng


class TrainingEngine:
    """Trains a real neural network from live browser data.

    The engine trains on the CPU by default (fast on modern multi-core CPUs)
    and automatically uses a CUDA-capable GPU if one is present. A GT 710
    (Kepler, 2 GB) is not supported by current PyTorch builds; when no
    supported GPU is found it falls back to CPU, which is both correct and
    typically faster for this tiny model.
    """

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self._cuda_available = torch.cuda.is_available()
        # Detect the actual GPU hardware for the dashboard note
        self._gpu_name = ''
        try:
            import subprocess as _sp
            out = _sp.run(['wmic', 'path', 'win32_videocontroller', 'get',
                           'Name'], capture_output=True, text=True,
                          timeout=5).stdout
            for line in out.splitlines():
                line = line.strip()
                if line and line != 'Name' and not line.startswith('Adapter'):
                    self._gpu_name = line.strip('\x00')
                    break
        except Exception:
            pass
        # mode: 'cpu', 'gpu', 'cpu+gpu'. If a supported GPU is not present,
        # any gpu mode transparently falls back to CPU.
        self.mode = 'gpu'
        self._set_device()
        self.n_features = 4
        self.model = BrowsingNet().to(self.device)
        self.optimizer = optim.Adam(self.model.parameters(), lr=0.01)
        self.loss_fn = nn.CrossEntropyLoss()
        # Use all available logical cores (Ryzen 5 5500 = 12 threads). This is
        # the real speed lever on this machine since there is no usable CUDA
        # GPU; letting PyTorch spread linear algebra across every core gives a
        # genuine multi-core speedup for both the classifier and word2vec.
        self.threads = max(2, min(12, torch.get_num_threads()))
        torch.set_num_threads(self.threads)

        self.checkpoint = self.data_dir / MODEL_PATH
        self.meta_file = self.data_dir / META_PATH
        self.lock = threading.Lock()

        self.epochs = 0
        self.examples_seen = 0
        self.last_train = None
        self.loss_history = []
        self.category_weights = {c: 0.5 for c in CATEGORIES}
        self._started_at = time.time()

        self._load()

    # ---------------------------------------------------------------- device/mode
    def _set_device(self):
        # The BrowsingNet classifier is a tiny net (a few KB of weights); it
        # trains in milliseconds on CPU. Keeping it there permanently reserves
        # the 4GB Radeon's VRAM for the 200-dim full-softmax word2vec, which is
        # the heavyweight that actually needs the GPU.
        self.device = torch.device('cpu')

    def set_mode(self, mode):
        """Set the training device mode: 'cpu', 'gpu' or 'cpu+gpu'. GPU modes
        prefer CUDA, then DirectML (e.g. the AMD Radeon RX 6400), then CPU.
        The word2vec text model honours the mode; the tiny BrowsingNet
        classifier always stays on CPU so the Radeon stays free.
        """
        mode = (mode or 'gpu').lower()
        if mode not in {'cpu', 'gpu', 'cpu+gpu'}:
            mode = 'gpu'
        if mode != self.mode:
            self.mode = mode
            self._set_device()
            self.model = self.model.to(self.device)
        return self.mode_info()

    def mode_info(self):
        on_dml = _is_dml_device(self.device)
        on_cuda = not on_dml and self.device.type == 'cuda'
        effective = 'gpu' if (on_dml or on_cuda) else 'cpu'
        gpu_name = _DML_NAME if on_dml else (self._gpu_name or '')
        if on_cuda:
            note = ('Using GPU (' + gpu_name + ').'
                    if torch.cuda.is_available() else 'Using GPU.')
        elif on_dml:
            note = ('Using ' + gpu_name + ' via DirectML (DirectX 12) '
                    'for training.')
        elif gpu_name:
            note = ('No supported GPU backend for ' + gpu_name +
                    '. Training uses CPU (Ryzen 5 5500, 12 threads).')
        else:
            note = 'No GPU found. Training uses CPU (Ryzen 5 5500, 12 threads).'
        return {
            'selected': self.mode,
            'effective': effective,
            'backend': 'cuda' if on_cuda else ('dml' if on_dml else 'cpu'),
            'cuda_available': self._cuda_available,
            'dml_available': _TORCH_DML_AVAILABLE,
            'gpu_name': gpu_name,
            'device': _device_label(self.device),
            'note': note,
        }

    # ---------------------------------------------------------------- helpers
    def _features(self, visit):
        """Convert a browser visit record into normalised model features."""
        ts = visit.get('timestamp') or datetime.now().isoformat()
        try:
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        except ValueError:
            dt = datetime.now()

        hour = (dt.hour + dt.minute / 60.0) / 24.0           # 0..1
        dow = dt.weekday() / 6.0                              # 0..1
        intensity = math.log1p(visit.get('visit_count', 1)) / 10.0
        mins_since = visit.get('mins_since_last', 24 * 60)
        recency = math.exp(-mins_since / (24 * 60))           # 1 = very recent

        return np.array([hour, dow, intensity, recency], dtype=np.float32)

    def _label_for(self, visit):
        cat = (visit.get('category') or 'general')
        if cat not in CATEGORIES:
            cat = 'general'
        return CATEGORIES.index(cat)

    def _build_dataset(self, visits):
        if not visits:
            return None, None
        X = np.array([self._features(v) for v in visits], dtype=np.float32)
        y = np.array([self._label_for(v) for v in visits], dtype=np.int64)
        e = np.array([float(v.get('engagement', 1.0)) for v in visits], dtype=np.float32)
        return X, (y, e)

    # ---------------------------------------------------------------- loading
    def _load(self):
        if not self.checkpoint.exists():
            return
        try:
            state = torch.load(self.checkpoint, map_location='cpu',
                               weights_only=False)
            self.model.load_state_dict(state['model'])
            # tensor weights were loaded on CPU; move to the active device
            self.model = self.model.to(self.device)
            for p in self.model.parameters():
                p.detach_()
            self.meta = state.get('meta', {})
            self.epochs = self.meta.get('epochs', 0)
            self.examples_seen = self.meta.get('examples_seen', 0)
            self.loss_history = self.meta.get('loss_history', [])
            self.category_weights.update(self.meta.get('category_weights', {}))
        except Exception as exc:  # a corrupt checkpoint should not kill the app
            print(f'[galaxypron] Could not load model checkpoint: {exc}')

    def _save(self):
        torch.save({
            'model': self.model.state_dict(),
            'meta': {
                'epochs': self.epochs,
                'examples_seen': self.examples_seen,
                'loss_history': self.loss_history[-200:],
                'category_weights': self.category_weights,
            }
        }, self.checkpoint)

    # ---------------------------------------------------------------- training
    def train(self, visits, epochs=8, batch_size=32):
        """Run a genuine gradient-descent training pass over the visits."""
        with self.lock:
            X, (y, e) = self._build_dataset(visits)
            if X is None or len(X) < 2:
                return {'trained': False, 'reason': 'not_enough_data'}

            X_t = torch.tensor(X).to(self.device)
            y_t = torch.tensor(y).to(self.device)
            e_t = torch.tensor(e).to(self.device)

            n = len(X_t)
            self.model.train()
            total_loss = 0.0
            batches = max(1, int(math.ceil(n / batch_size)))
            steps = 0

            for _ in range(epochs):
                perm = torch.randperm(n)
                for i in range(batches):
                    idx = perm[i * batch_size:(i + 1) * batch_size]
                    if idx.numel() == 0:
                        continue
                    xb, yb, eb = X_t[idx], y_t[idx], e_t[idx]

                    self.optimizer.zero_grad()
                    cls_logits, eng = self.model(xb)
                    cls_loss = self.loss_fn(cls_logits, yb)
                    eng_loss = nn.functional.mse_loss(eng.squeeze(), eb)
                    loss = cls_loss + 0.5 * eng_loss
                    loss.backward()
                    self.optimizer.step()
                    total_loss += loss.item()
                    steps += 1

            self.epochs += epochs
            self.examples_seen += n
            self.last_train = datetime.now().isoformat()
            avg_loss = total_loss / max(steps, 1)
            self.loss_history.append(round(avg_loss, 5))

            self._refresh_category_weights(X_t, y_t)
            self._save()

            return {
                'trained': True,
                'epochs': epochs,
                'examples': n,
                'avg_loss': round(avg_loss, 5),
                'device': str(self.device),
            }

    def _refresh_category_weights(self, X_t, y_t):
        """Update learned per-category engagement weights from the model."""
        self.model.eval()
        with torch.no_grad():
            cls_logits, eng = self.model(X_t)
            probs = torch.softmax(cls_logits, dim=1)
            for ci in range(len(CATEGORIES)):
                mask = y_t == ci
                if mask.any():
                    w = eng.squeeze()[mask].mean().item()
                    self.category_weights[CATEGORIES[ci]] = round(w, 3)

    # ---------------------------------------------------------------- inference
    def predict_category(self, visit):
        """Return (category, confidence) the model believes a visit belongs to."""
        self.model.eval()
        x = torch.tensor(self._features(visit)).unsqueeze(0).to(self.device)
        with torch.no_grad():
            logits, _ = self.model(x)
            probs = torch.softmax(logits, dim=1)[0]
        idx = int(probs.argmax().item())
        return CATEGORIES[idx], float(probs[idx].item())

    def preferred_categories(self, top=3):
        cats = sorted(CATEGORIES,
                      key=lambda c: self.category_weights.get(c, 0.5),
                      reverse=True)
        return [{'category': c, 'engagement': round(self.category_weights.get(c, 0.5), 3)}
                for c in cats[:top]]

    # ---------------------------------------------------------------- stats
    def stats(self):
        return {
            'device': _device_label(self.device),
            'threads': self.threads,
            'model': 'BrowsingNet v1',
            'mode': self.mode_info(),
            'trainable_params': sum(p.numel() for p in self.model.parameters()
                                    if p.requires_grad),
            'epochs': self.epochs,
            'examples_seen': self.examples_seen,
            'loss_history': self.loss_history[-20:],
            'last_train': self.last_train,
            'category_weights': dict(self.category_weights),
            'uptime_seconds': round(time.time() - self._started_at),
        }

# ------------------------------------------------------------- TextModel
# A separate, genuinely-learning word2vec (skip-gram) text model. Unlike the
# small BrowsingNet category classifier, this model learns distributed word
# vectors by reading every Wikipedia article the continuous trainer delivers.
# Vocabulary grows online; every article drives real SGD steps whose progress
# (epochs, examples, words, loss, vocab) is surfaced on the model panel.


class Word2Vec(nn.Module):
    """Skip-gram with full softmax over the vocabulary.

    Normal-size (200-dim) in/out embedding tables. The output matmul
    (batch x dim) x (vocab x dim).T is a real GPU workload — the forward
    pass alone is a [batch x V] dense multiplication that saturates the
    Radeon instead of the tiny gather-and-dot tricks of negative sampling.
    """

    def __init__(self, vocab_size, embed_dim=200):
        super().__init__()
        self.embed_dim = embed_dim
        self.embed = nn.Embedding(vocab_size, embed_dim)
        self.out_embed = nn.Embedding(vocab_size, embed_dim)
        initrange = 0.5 / embed_dim
        nn.init.uniform_(self.embed.weight, -initrange, initrange)
        nn.init.uniform_(self.out_embed.weight, -initrange, initrange)

    def forward(self, centers):
        e_c = self.embed(centers)                            # (N, d)
        logits = torch.matmul(e_c, self.out_embed.weight.t())  # (N, V)
        return logits


class TextModel:
    """Word2vec text model that learns from every ingested Wikipedia article."""

    def __init__(self, data_dir, embed_dim=200, window=2, neg_samples=2, device='cpu',
             vocab_cap=None, tier=None):
        self.data_dir = Path(data_dir)
        self.tier = tier
        self.vocab_file = self.tier.cache(TEXT_VOCAB_PATH) if self.tier else \
            self.data_dir / TEXT_VOCAB_PATH
        self.checkpoint = self.tier.cache(TEXT_MODEL_PATH) if self.tier else \
            self.data_dir / TEXT_MODEL_PATH
        self.embed_dim = embed_dim
        self.window = window
        self.neg_samples = neg_samples
        self.device = torch.device(device)
        # vocab_cap freezes the embedding tables at one fixed size. The 4GB
        # Radeon + torch-directml's never-reusing allocator cannot survive
        # endless rebuilds as the vocabulary grows, so in production we build
        # the model once and map anything beyond the cap to <unk>.
        self.vocab_cap = vocab_cap
        self.softmax_batch = 256   # pairs per optimizer step (fits in 4GB VRAM)
        self.text_mode = 'gpu'
        self._gpu_name = ''
        try:
            import subprocess as _sp
            out = _sp.run(['wmic', 'path', 'win32_videocontroller', 'get',
                           'Name'], capture_output=True, text=True,
                          timeout=5).stdout
            for line in out.splitlines():
                line = line.strip()
                if line and line != 'Name' and not line.startswith('Adapter'):
                    self._gpu_name = line.strip('\x00')
                    break
        except Exception:
            pass

        self.word2id = {'<unk>': 0}
        self.id2word = ['<unk>']
        self.vocab_size = 1
        self.model = None
        self.optimizer = None

        self.epochs = 0
        self.examples_seen = 0
        self.words_trained = 0
        self.last_train = None
        self.loss_history = []
        self._started_at = time.time()

        self._load()

    # ------------------------------------------------------------ device/mode
    def set_mode(self, mode):
        """Set the device for the 24/7 word2vec model: 'cpu', 'gpu' or
        'cpu+gpu'. GPU modes use CUDA first, then DirectML (AMD/Intel
        DirectX 12 GPUs), then CPU. GPU remains the default."""
        mode = (mode or 'gpu').lower()
        if mode not in {'cpu', 'gpu', 'cpu+gpu'}:
            mode = 'gpu'
        self.text_mode = mode
        cuda_ok = torch.cuda.is_available()
        if mode in ('gpu', 'cpu+gpu') and cuda_ok:
            new_dev = torch.device('cuda')
        elif mode in ('gpu', 'cpu+gpu', 'dml') and _TORCH_DML_AVAILABLE:
            new_dev = _DML_DEVICE
        else:
            new_dev = torch.device('cpu')
        if _device_label(new_dev) != _device_label(self.device):
            self.device = new_dev
            if self.model is not None:
                with torch.no_grad():
                    self.model = self.model.to(self.device)
                # rebuild the optimiser so its state lands on the new device
                self.optimizer = self._make_optimizer()
        return self.mode_info()

    def mode_info(self):
        on_dml = _is_dml_device(self.device)
        on_cuda = not on_dml and self.device.type == 'cuda'
        effective = 'gpu' if (on_dml or on_cuda) else 'cpu'
        gpu_name = _DML_NAME if on_dml else (self._gpu_name or '')
        if on_cuda:
            note = ('Using GPU (' + gpu_name + ') for the 24/7 text model.'
                    if torch.cuda.is_available()
                    else 'Using GPU for the 24/7 text model.')
        elif on_dml:
            note = ('Using ' + gpu_name + ' via DirectML (DirectX 12) '
                    'for the 24/7 text model.')
        elif gpu_name:
            note = ('No supported GPU backend for ' + gpu_name +
                    '. 24/7 text training runs on CPU.')
        else:
            note = '24/7 text training on CPU (no GPU found).'
        return {
            'selected': self.text_mode,
            'effective': effective,
            'backend': 'cuda' if on_cuda else ('dml' if on_dml else 'cpu'),
            'cuda_available': torch.cuda.is_available(),
            'dml_available': _TORCH_DML_AVAILABLE,
            'gpu_name': gpu_name,
            'device': _device_label(self.device),
            'note': note,
        }

    # ------------------------------------------------------------ vocab
    def _add_word(self, word):
        if word not in self.word2id:
            self.word2id[word] = self.vocab_size
            self.id2word.append(word)
            self.vocab_size += 1

    def _make_optimizer(self):
        """On the DirectML backend Adam's momentum step falls back to the CPU
        (aten::lerp.Scalar_out unsupported), which roughly halves throughput.
        SGD-momentum keeps every op on the GPU, so DML word2vec runs ~2x
        faster than it would with Adam."""
        if _is_dml_device(self.device):
            return optim.SGD(self.model.parameters(), lr=0.05, momentum=0.9)
        return optim.Adam(self.model.parameters(), lr=0.01)

    def _fit_softmax_batch(self, cap_mb=256):
        # keep logits for one batch under ~cap_mb so there is headroom for
        # the backwards pass on the 4GB Radeon (the BrowsingNet classifier
        # also lives on the card; vocab grows over time, so the batch must
        # shrink with it). Sizing from the fixed table size keeps the batch
        # stable even as the vocabulary grows.
        cap = max(16, int((cap_mb << 20) // (4 * max(self._model_vocab(), 1))))
        self.softmax_batch = min(512, cap)

    def _model_vocab(self):
        """Size of the embedding tables: always the full fixed cap when one is
        set (tables are sized once up front and never rebuilt), else exact."""
        if self.vocab_cap:
            return self.vocab_cap
        return self.vocab_size

    def _ensure_model(self):
        target = self._model_vocab()
        if self.model is None or target > self.model.embed.num_embeddings:
            old = self.model
            new = Word2Vec(target, self.embed_dim).to(self.device)
            if old is not None:
                old_n = old.embed.num_embeddings
                copy = min(old_n, self.vocab_size)
                with torch.no_grad():
                    new.embed.weight[:copy] = old.embed.weight.detach()[:copy]
                    new.out_embed.weight[:copy] = old.out_embed.weight.detach()[:copy]
            self.model = new
            self.optimizer = self._make_optimizer()
            self._fit_softmax_batch()

    # ------------------------------------------------------------ training
    def learn_article(self, title, extract):
        import re
        text = f'{title} {extract}'
        tokens = re.findall(r"[a-zA-Z']+", text.lower())
        if len(tokens) < 2:
            return 0

        for w in tokens:
            self._add_word(w)
        ids = [self.word2id[w] for w in tokens]
        self._ensure_model()
        if self.vocab_cap:
            ids = [i if i < self.vocab_cap else 0 for i in ids]  # 0 == <unk>

        pairs = []
        for i, c in enumerate(ids):
            lo = max(0, i - self.window)
            hi = min(len(ids), i + self.window + 1)
            for j in range(lo, hi):
                if j != i:
                    pairs.append((c, ids[j]))
        if not pairs:
            return 0

        center = torch.tensor([c for c, _ in pairs], dtype=torch.long).to(self.device)
        context = torch.tensor([p for _, p in pairs], dtype=torch.long).to(self.device)
        n = len(pairs)

        self.model.train()
        total_loss = 0.0
        steps = 0
        batch = self.softmax_batch
        try:
            for s in range(0, n, batch):
                cb = center[s:s + batch]
                xb = context[s:s + batch]
                self.optimizer.zero_grad()
                logits = self.model(cb)
                loss = nn.functional.cross_entropy(logits, xb)
                loss.backward()
                self.optimizer.step()
                total_loss += loss.detach() * cb.numel()   # no per-step GPU->CPU sync
                steps += 1
        except RuntimeError:
            # out-of-VRAM: shrink the batch so the next attempt fits and the
            # model keeps learning at whatever size the card allows; give up
            # (return 0) only once the floor is reached
            self.softmax_batch = max(16, int(self.softmax_batch * 0.75))
            return 0

        self.examples_seen += n
        self.words_trained += len(tokens)
        self.epochs += 1
        self.last_train = datetime.now().isoformat()
        avg = float(total_loss / max(n, 1))
        self.loss_history.append(round(avg, 5))
        self.loss_history = self.loss_history[-40:]

        if self.words_trained % 5000 < len(tokens):
            self._save()

        return n

    # ------------------------------------------------------------ persistence
    def _save(self):
        try:
            torch.save({'model': self.model.state_dict()}, self.checkpoint)
            torch.save({'word2id': self.word2id,
                        'vocab_size': self.vocab_size,
                        'epochs': self.epochs,
                        'examples_seen': self.examples_seen,
                        'words_trained': self.words_trained,
                        'loss_history': self.loss_history,
                        'last_train': self.last_train},
                       self.vocab_file)
            if self.tier:
                self.tier.schedule(TEXT_MODEL_PATH)
                self.tier.schedule(TEXT_VOCAB_PATH)
        except Exception:
            pass

    def _load(self):
        try:
            cp = self.tier.read(TEXT_MODEL_PATH) if self.tier else self.checkpoint
            vf = self.tier.read(TEXT_VOCAB_PATH) if self.tier else self.vocab_file
            if cp.exists():
                state = torch.load(cp, map_location='cpu',
                                   weights_only=False)
                meta = torch.load(vf, map_location='cpu',
                                  weights_only=False) \
                    if vf.exists() else {}
                self.word2id = meta.get('word2id', {'<unk>': 0})
                self.vocab_size = len(self.word2id)
                self.model = Word2Vec(self._model_vocab(), self.embed_dim).to(self.device)
                self.optimizer = self._make_optimizer()
                try:
                    self.model.load_state_dict(state['model'])
                except RuntimeError:
                    # architecture changed (e.g. 48-dim -> 200-dim): keep the
                    # vocabulary, retrain embeddings from scratch
                    print('[galaxypron] word2vec architecture changed; '
                          'starting fresh embeddings')
                self._fit_softmax_batch()
                self.epochs = meta.get('epochs', 0)
                self.examples_seen = meta.get('examples_seen', 0)
                self.words_trained = meta.get('words_trained', 0)
                self.loss_history = meta.get('loss_history', [])
                self.last_train = meta.get('last_train', None)
        except Exception:
            pass

    # ------------------------------------------------------------ progress
    def progress(self):
        return {
            'vocab_size': self.vocab_size,
            'embed_dim': self.embed_dim,
            'epochs': self.epochs,
            'examples_seen': self.examples_seen,
            'words_trained': self.words_trained,
            'loss_history': self.loss_history[-12:],
            'last_train': self.last_train,
            'uptime_seconds': round(time.time() - self._started_at),
        }
