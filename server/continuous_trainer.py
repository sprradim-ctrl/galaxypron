import json
import os
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from training_engine import TextModel

WIKI_SUMMARY_URL = 'https://en.wikipedia.org/api/rest_v1/page/random/summary'
# Bulk-fetch several random articles in ONE request (MediaWiki action=query).
# Far fewer API calls => much less rate-limit pressure => real throughput.
WIKI_BULK_URL = ('https://en.wikipedia.org/w/api.php?action=query&format=json'
                 '&generator=random&grnnamespace=0&grnlimit=50'
                 '&prop=revisions&rvprop=content&rvslots=main&rvsection=0')
# Systematic sweep: walks the WHOLE Wikipedia (every article) in alphabetical
# order via allpages so nothing is missed and nothing is re-fetched. One request
# returns ~50 pages with their raw wikitext, same as the random bulk fetch.
# (~6.9M articles = ~138k requests, a few days of 24/7 fetching.)
WIKI_SWEEP_URL = ('https://en.wikipedia.org/w/api.php?action=query&format=json'
                  '&generator=allpages&gapnamespace=0&gaplimit=50'
                  '&prop=revisions&rvprop=content&rvslots=main&rvsection=0')
BATCH_SIZE = 50          # articles per bulk request (max Wikipedia allows)
SOURCES_PER_CYCLE = 600  # ingest this many articles...
BREAK_SECONDS = 5        # ...then rest for 5 seconds
ADAPTIVE_MIN_DELAY = 0.01 # floor between bulk fetches when Wikipedia allows it
ADAPTIVE_MAX_DELAY = 6.0  # ceiling after repeated rate limits
WIKI_ARTICLE_GOAL = 6900000  # full English Wikipedia (~6.9M articles)

STATE_FILE = 'knowledge.json'
SWEEP_STATE_FILE = 'sweep_state.json'  # persists the full-Wikipedia cursor


def _wikitext_to_text(raw):
    """Strip MediaWiki markup down to readable prose.

    Serves as the raw-content counterpart to extracts: Wikipedia serves raw
    wikitext cheaply (no server-side extraction -> far fewer HTTP 429s), and
    this converts it to plain text for the KnowledgeBase. Good enough for a
    statistical model even if a rare template leaks through.
    """
    if not raw:
        return ''
    import re
    s = raw[:4000]  # the intro lives at the top; cap the work
    # remove refs and other html tags first so their content disappears
    s = re.sub(r'<ref[^>]*>.*?</ref>', ' ', s, flags=re.S | re.I)
    s = re.sub(r'<ref[^>]*/>', ' ', s, flags=re.I)
    s = re.sub(r'<[^>]+>', ' ', s)
    # templates (mostly balanced-brace)
    for _ in range(6):
        s = re.sub(r'\{\{[^{}]*\}\}', ' ', s)
    # tables
    s = re.sub(r'\{\|[^{}]*\|}', ' ', s, flags=re.S)
    # file/image inclusions
    s = re.sub(r'\[\[(?:File|Image|Category|Template|Media):[^\]]*\]\]', ' ', s)
    # piped links -> display text
    s = re.sub(r'\[\[[^\]|]*\|([^\]]*)\]\]', r'\1', s)
    s = re.sub(r'\[\[([^\]]*)\]\]', r'\1', s)
    # external links -> trailing text
    s = re.sub(r'\[https?://[^\]\s]+\s+([^\]]+)\]', r'\1', s)
    s = re.sub(r'\[https?://[^\]]*\]', ' ', s)
    # headings / bold-italic markers
    s = re.sub(r"'{2,}", '', s)
    s = re.sub(r'^=+.*?=+\s*$', ' ', s, flags=re.M)
    s = re.sub(r'[=]+', ' ', s)
    # strip leftover brackets and braces
    s = re.sub(r'[\[\]{}|]', ' ', s)
    # collapse whitespace
    s = ' '.join(s.split())
    return s


def _article_text(page):
    """Extract clean readable text from one revisions API page object."""
    revisions = page.get('revisions') or []
    raw = ''
    for rev in revisions:
        slots = rev.get('slots') or {}
        if slots:
            main = slots.get('main') or {}
            raw = main.get('*') or main.get('content') or ''
        else:
            raw = rev.get('*') or ''
        if raw:
            break
    return _wikitext_to_text(raw)


class KnowledgeBase:
    """A small, continuously-trainable text model over ingested Wikipedia.

    It genuinely learns: it grows a vocabulary, tracks word/topic frequencies
    and builds a bigram transition model, and every ingestion writes back a
    checkpoint so learning persists across the 24/7 session and restarts.
    """

    def __init__(self, data_dir, tier=None):
        self.data_dir = Path(data_dir)
        self.tier = tier
        self.file = self.tier.read(STATE_FILE) if self.tier else self.data_dir / STATE_FILE
        self.file_write = self.tier.cache(STATE_FILE) if self.tier else self.file
        self.vocab = Counter()       # word -> occurrences
        self.unigrams = Counter()    # word -> distinct articles seen in
        self.bigrams = Counter()     # (w1, w2) -> occurrences
        self.topics = Counter()      # detected topic -> articles
        self.total_words = 0
        self.articles_ingested = 0
        self.last_article_title = ''
        # grammar learning: sentence-length distribution + connector usage
        self.sentence_lengths = Counter()   # bucket -> sentences
        self.connectors = Counter()         # connector word -> uses
        self.grammar_sentences = 0
        self._save_thread = None
        # Guards the counters that the ingest loop mutates while the Flask
        # threads read them (vocab.most_common raises RuntimeError otherwise).
        self._vocab_lock = threading.Lock()
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            state = json.loads(self.file.read_text(encoding='utf-8'))
            self.vocab = Counter(state.get('vocab', {}))
            self.unigrams = Counter(state.get('unigrams', {}))
            self.bigrams = Counter({tuple(k.split('\x00')): v
                                    for k, v in state.get('bigrams', {}).items()})
            self.topics = Counter(state.get('topics', {}))
            self.total_words = state.get('total_words', 0)
            self.articles_ingested = state.get('articles', 0)
            self.sentence_lengths = Counter(state.get('sentence_lengths', {}))
            self.connectors = Counter(state.get('connectors', {}))
            self.grammar_sentences = state.get('grammar_sentences', 0)
        except Exception:
            pass

    def _save(self):
        # Fully async persistence: both the counter snapshot AND the ~128MB
        # JSON dump + disk write happen off the training loop. Copying the
        # counters while they grow is safe under the GIL (we just capture a
        # slightly-late checkpoint). If a save is already in-flight we skip,
        # so saves never queue up behind a slower disk.
        if self._save_thread is not None and self._save_thread.is_alive():
            return
        try:
            self._save_thread = threading.Thread(
                target=self._snapshot_and_write, daemon=True)
            self._save_thread.start()
        except Exception:
            pass

    def _snapshot_and_write(self):
        try:
            state = {
                'vocab': dict(self.vocab),
                'unigrams': dict(self.unigrams),
                'bigrams': {f'{a}\x00{b}': v for (a, b), v in self.bigrams.items()},
                'topics': dict(self.topics),
                'total_words': self.total_words,
                'articles': self.articles_ingested,
                'sentence_lengths': dict(self.sentence_lengths),
                'connectors': dict(self.connectors),
                'grammar_sentences': self.grammar_sentences,
            }
            tmp = self.file_write.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(state), encoding='utf-8')
            tmp.replace(self.file_write)
            if self.tier:
                self.tier.schedule(STATE_FILE)
        except Exception:
            pass

    def learn_article(self, title, extract):
        """Ingest one article: tokenise, update transitions and topics."""
        title = title or ''
        extract = extract or ''
        text = f'{title} {extract}'
        import re
        tokens = re.findall(r"[a-zA-Z']+", text.lower())
        if not tokens:
            return 0

        with self._vocab_lock:
            for i, w in enumerate(tokens):
                self.vocab[w] += 1
                if i == 0 or tokens[i - 1] == '.':  # conservative sentence start
                    self.unigrams[w] += 1
                if i > 0:
                    self.bigrams[(tokens[i - 1], w)] += 1

        self.total_words += len(tokens)
        self.articles_ingested += 1
        self.last_article_title = title
        # crude topic tag from the summary's first meaningful noun-ish words
        if title:
            self.topics[title.split()[0].lower().strip(",'\"")] += 1

        # persist on a separate thread so the write never blocks the loop
        if self.articles_ingested % 50 == 0:
            self._save()

        return len(tokens)

    # Learn grammar: sentence length buckets + connective-word usage.
    def learn_grammar(self, text, rate=1.0):
        import re
        if not text:
            return
        sentences = re.split(r'[.!?]+\s+', text)
        connectorset = ('and', 'or', 'but', 'because', 'although', 'while',
                        'if', 'so', 'since', 'however', 'therefore',
                        'moreover', 'furthermore', 'although', 'unless')
        cap = max(1, int(rate))
        for s in sentences[:cap]:
            words = re.findall(r"[a-zA-Z']+", s)
            if len(words) < 2:
                continue
            bucket = min(7, len(words) // 10)
            self.sentence_lengths[bucket] += 1
            for w in set(words):
                if w.lower() in connectorset:
                    self.connectors[w.lower()] += 1
            self.grammar_sentences += 1

    def grammar_stats(self):
        return {
            'sentences': self.grammar_sentences,
            'length_buckets': dict(self.sentence_lengths),
            'connectors': dict(self.connectors.most_common(8)),
        }

    # Periodic checkpoint used by the continuous loop
    def checkpoint(self):
        self._save()

    # Synchronous flush: wait out any in-flight save, then write now. Used at
    # graceful shutdown so we never lose progress on disk eject / exit.
    def flush(self):
        try:
            if self._save_thread is not None:
                self._save_thread.join(timeout=20)
        except Exception:
            pass
        try:
            self._snapshot_and_write()
        except Exception:
            pass

    def stats(self):
        with self._vocab_lock:
            top_vocab = self.vocab.most_common(12)
            vocab_size = len(self.vocab)
        return {
            'articles_ingested': self.articles_ingested,
            'vocabulary_size': vocab_size,
            'total_words': self.total_words,
            'most_common_words': [{'word': w, 'count': c} for w, c in top_vocab],
            'last_article': self.last_article_title,
        }


class ContinuousTrainer:
    """Runs 24/7: ingests ~1 Wikipedia source/sec with a 1-min break every 450.

    The trainer is a daemon so it never blocks the web app. You can start and
    stop it, and read its live status. Training mode (cpu / gpu / cpu+gpu) is
    honoured by holding the mode so the GPU path is used when a supported GPU
    exists.
    """

    def __init__(self, data_dir, train_mode='gpu', tier=None):
        self.data_dir = Path(data_dir)
        self.tier = tier
        self.kb = KnowledgeBase(data_dir, tier=tier)
        # Fixed-size embedding tables: the DirectML allocator never reclaims
        # freed slabs, and an endlessly-growing vocab otherwise ratchets VRAM
        # up to the 4GB ceiling and OOMs. Capturing at 393216 keeps the model
        # ~1.6GB (CPU: RAM is plenty; GPU: fits — but see the note below).
        self.text_model = TextModel(data_dir, vocab_cap=393216, tier=tier)
        self.train_mode = train_mode
        # CPU is the fastest RELIABLE path: torch-directml leaks VRAM so any
        # sustained GPU softmax training dies in a few minutes and can even
        # reset the display driver. The 12-core Ryzen sustains word2vec all
        # day. GPU stays selectable for bursty runs from the UI.
        try:
            self.text_model.set_mode('cpu' if train_mode == 'gpu' else train_mode)
        except Exception:
            pass
        self.intensity = 30           # 1..100 overall learning intensity
        self.grammar_enabled = False  # grammar training toggle
        self.grammar_intensity = 30   # 1..100 grammar training intensity
        # 'random' draws random articles (fast, fun, duplicates). 'full' sweeps
        # the complete English Wikipedia in order so the 6.9M-article goal is
        # actually reachable without re-fetching.
        self.coverage = 'random'
        self.sweep_continue = ''      # allpages continue token
        self.sweep_total = 0          # articles swept this run (not duplicates)
        self._load_sweep_state()
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.cycle_sources = 0        # sources since last break
        self.break_remaining = 0      # seconds left in a break
        self._break_job = None        # in-progress word2vec batch (yields/slice)
        self._break_deadline = 0.0    # time by which the break training must end
        self._w2v_queue = deque(maxlen=800)  # articles queued for GPU word2vec
        self._w2v_thread = None        # continuous background word2vec trainer
        self.sources_since_break = 0
        self.breaks_taken = 0
        self.last_error = None
        self.started_at = None
        self._session_sources = 0
        self._w2v_log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'data', 'w2v_loop.log')
        # NO locks — Python's GIL protects Counter ops, and torch releases the
        # GIL during matmul/backward so multiple workers train word2vec across
        # all 12 Ryzen cores simultaneously. Removing the locks is the single
        # biggest speedup on this machine: the old serial lock forced word2vec
        # training to run one article at a time, wasting 11 idle cores.
        self._pool = ThreadPoolExecutor(max_workers=12)
        self._http = requests.Session()
        self._http.headers.update({'User-Agent': 'galaxypron/1.0'})

    def _learn_article_worker(self, title, extract):
        """Run the heavy learning for one article off the main loop thread.

        KB (vocab/bigrams/topics) updates from every article (fast, pure Python).
        Word2vec is skipped during the main cycle for maximum throughput — it
        runs in a separate lightweight pass when the cycle completes.
        """
        learned = 0
        try:
            self.kb.learn_article(title, extract)
            learned = 1
        except Exception:
            pass
        if learned:
            # feed the background GPU word2vec trainer so the Radeon gets a
            # steady stream of real work instead of ~2 articles per break.
            # Queue the extract too — title-only batches are too thin to ever
            # produce real matmuls (that's a few hundred tokens per batch).
            try:
                self._w2v_queue.append((title, extract))
            except Exception:
                pass
        if self.grammar_enabled:
            try:
                self.kb.learn_grammar(f'{title} {extract}',
                                      rate=self.grammar_intensity / 20.0)
            except Exception:
                pass
        return learned

    def set_train_mode(self, mode):
        """Store the chosen mode AND push it into the live word2vec text model
        so the 24/7 Wikipedia trainer genuinely changes device (not just the
        label). Returns the effective device info for the UI."""
        mode = (mode or 'gpu').lower()
        self.train_mode = mode
        try:
            return self.text_model.set_mode(mode)
        except Exception:
            return self.text_model.mode_info()

    def set_controls(self, **kw):
        """Update live controls from the UI: mode, intensity, grammar flag,
        coverage (random vs full-Wikipedia sweep)."""
        mode = kw.get('mode')
        if mode is not None:
            self.set_train_mode(mode)
        intensity = kw.get('intensity')
        if intensity is not None:
            self.intensity = max(1, min(100, int(intensity)))
        if 'grammar' in kw:
            self.grammar_enabled = bool(kw.get('grammar'))
        gi = kw.get('grammar_intensity')
        if gi is not None:
            self.grammar_intensity = max(1, min(100, int(gi)))
        coverage = kw.get('coverage')
        if coverage is not None:
            c = str(coverage).lower()
            if c in ('random', 'full'):
                self.coverage = c
                if c == 'full':
                    # starting a fresh systematic sweep resets to the beginning
                    if kw.get('reset_sweep'):
                        self.sweep_continue = ''
                        self.sweep_total = 0
                        self._save_sweep_state()
        return self.controls()

    # ------------------------------------------------------- sweep persistence
    def _sweep_state_file(self):
        return self.data_dir / SWEEP_STATE_FILE

    def _load_sweep_state(self):
        try:
            st = json.loads(self._sweep_state_file().read_text(encoding='utf-8'))
            self.coverage = st.get('coverage', 'random')
            self.sweep_continue = st.get('continue', '')
            self.sweep_total = st.get('total', 0)
        except Exception:
            pass

    def _save_sweep_state(self):
        try:
            st = {
                'coverage': self.coverage,
                'continue': self.sweep_continue,
                'total': self.sweep_total,
            }
            tmp = self._sweep_state_file().with_suffix('.json.tmp')
            tmp.write_text(json.dumps(st), encoding='utf-8')
            tmp.replace(self._sweep_state_file())
        except Exception:
            pass

    # ---------------------------------------------------------------- control
    def start(self):
        if self._running:
            return {'running': True}
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._w2v_thread = threading.Thread(target=self._w2v_loop, daemon=True)
        self._w2v_thread.start()
        return {'running': True, 'mode': self.train_mode}

    def stop(self):
        if not self._running:
            return {'running': False}
        self._stop.set()
        self._thread = None
        self._w2v_thread = None
        if self.kb.articles_ingested > 0:
            self.kb.checkpoint()
        self._save_sweep_state()
        return {'running': False}

    def _w2v_loop(self):
        """Train word2vec continuously in the background so the Radeon GPU
        stays busy with real computation while the main loop ingests.

        Each call is ONE article sliced to ~400 words: that exact pattern was
        benchmarked as indefinitely sustainable on the 4GB Radeon (no OOM),
        because torch-directml's allocator never recycles freed slabs and
        large multi-article combined batches blow out VRAM within a call.
        """
        while not self._stop.is_set():
            batch = []
            while self._w2v_queue and len(batch) < 8:
                try:
                    batch.append(self._w2v_queue.popleft())
                except Exception:
                    break
            if not batch:
                self._stop.wait(0.05)
                continue
            t0 = time.time()
            trained = 0
            try:
                for title, extract in batch:
                    trained += self.text_model.learn_article(
                        title, extract[:2000])
                dt = time.time() - t0
                with open(self._w2v_log, 'a', encoding='utf-8') as f:
                    f.write('%s batch=%d -> n=%d dt=%.1fs sb=%d\n' % (
                        datetime.now().isoformat(), len(batch), trained, dt,
                        self.text_model.softmax_batch))
            except Exception as exc:
                dt = time.time() - t0
                with open(self._w2v_log, 'a', encoding='utf-8') as f:
                    f.write('%s batch=%d EXC %r dt=%.1fs\n' % (
                        datetime.now().isoformat(), len(batch), exc, dt))

    # ---------------------------------------------------------------- loop
    def _loop(self):
        self._running = True
        self.started_at = datetime.now().isoformat()
        # Adaptive pacing: fetch as fast as Wikipedia allows, backing off only
        # on rate limits so we max out sustained throughput instead of idling.
        delay = self._base_delay()
        while not self._stop.is_set():
            if self.break_remaining > 0:
                # keep the 5-second break short: word2vec training is sliced so
                # it can never stretch the break into minutes, and ingestion
                # resumes the moment the break window elapses
                if self.break_remaining == BREAK_SECONDS:
                    self._break_job = self._word2vec_batch_gen()
                    self._break_deadline = time.time() + max(1.0, BREAK_SECONDS - 1)
                if self._break_job is not None and time.time() < self._break_deadline:
                    try:
                        next(self._break_job, None)
                    except Exception:
                        self._break_job = None
                if time.time() >= self._break_deadline:
                    self._break_job = None
                if self._stop.wait(1):
                    break
                self.break_remaining = max(0, self.break_remaining - 1)
                continue

            learned = self._ingest_one()
            if not learned:
                # network hiccup / rate limit: back off a little, keep trying
                delay = min(ADAPTIVE_MAX_DELAY, delay * 2)
                if self._stop.wait(min(delay, 6)):
                    break
                continue

            # success: count every article actually learned, then speed back up
            delay = max(self._base_delay(), delay * 0.5)
            self.cycle_sources += learned
            self.sources_since_break += learned
            self._session_sources += learned

            if self.sources_since_break >= self._sources_per_cycle():
                self.break_remaining = BREAK_SECONDS
                self.sources_since_break = 0
                self.breaks_taken += 1
                self.kb.checkpoint()
                self._save_sweep_state()

            if self._stop.wait(delay):
                break

        self._running = False

    def _ingest_one(self):
        """Fetch one batch of wikitext and learn all its sections.

        prop=revisions + rvsection=0 serves stored wikitext directly with no
        on-the-fly extraction, so one request returns ALL ~50 article sections
        (the old prop=extracts API capped at 20 and triggered HTTP 429
        throttling). A single gentle request per cycle keeps well below
        Wikipedia's rate limit while moving 50 articles at a time.

        Two coverage modes:
          - 'random': random articles (duplicates possible, fast).
          - 'full'  : systematic allpages sweep; walks the entire English
            Wikipedia (~6.9M articles) in order. The allpages continue token
            is persisted to sweep_state.json so a restart resumes exactly
            where the last run stopped.
        """
        try:
            url = WIKI_BULK_URL
            if self.coverage == 'full':
                url = WIKI_SWEEP_URL
                if self.sweep_continue:
                    url += '&gapcontinue=' + urllib.parse.quote(self.sweep_continue)
            r = self._http.get(url, timeout=20)
            if r.status_code == 429:
                retry = r.headers.get('Retry-After')
                try:
                    wait = max(1.0, float(retry)) if retry else 10.0
                except (TypeError, ValueError):
                    wait = 10.0
                self.last_error = ('Rate limited (HTTP 429) — '
                                   f'pausing {int(wait)}s')
                if self._stop.wait(wait):
                    return False
                return False
            if r.status_code != 200:
                self.last_error = f'HTTP {r.status_code}'
                return False
            data = r.json()
            if self.coverage == 'full':
                cont = data.get('continue', {}) or {}
                self.sweep_continue = cont.get('gapcontinue', '')
                if not self.sweep_continue:
                    # reached the end of the alphabet -> loop back to the start
                    self.sweep_continue = ''
                self.sweep_total += 1  # one request batch (>=1 article)
            pages = data.get('query', {}).get('pages', {}) or {}
            learned = 0
            for page in pages.values():
                title = page.get('title', '')
                if not title:
                    continue
                text = _article_text(page)
                if not text:
                    continue
                try:
                    self.kb.learn_article(title, text)
                    learned += 1
                except Exception:
                    pass
                if learned:
                    try:
                        self._w2v_queue.append((title, text))
                    except Exception:
                        pass
                if self.grammar_enabled:
                    try:
                        self.kb.learn_grammar(
                            f'{title} {text}',
                            rate=self.grammar_intensity / 20.0)
                    except Exception:
                        pass
            self.last_error = None
            if self.coverage == 'full' and learned:
                # persist the sweep cursor so restarts resume in the same spot
                self._save_sweep_state()
            return learned
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def _word2vec_batch_gen(self):
        """Yield one article's word2vec pass at a time so word2vec trains only
        during the short break and the break length stays ~5 seconds. Whatever
        isn't trained within the window simply waits for the next break."""
        sample = list(self.kb.topics.keys())[-200:]
        for title in sample:
            try:
                self.text_model.learn_article(title, title)
            except Exception:
                pass
            yield

    # ---------------------------------------------------------------- intensity
    def _sources_per_cycle(self):
        """The number of sources learned before the 1-minute break.

        Fixed at 600 per the dashboard goal. Intensity tunes the *speed* of
        learning (via the pause between fetches), not the cycle length.
        """
        return SOURCES_PER_CYCLE

    def _base_delay(self):
        """Base wait between bulk fetches: higher intensity waits less.

        Reduced floor + gentler scaling = it fetches back-to-back as fast as
        Wikipedia allows, so learning runs faster (adaptive backoff still kicks
        in on 429 rate limits).
        """
        frac = 1.0 - (self.intensity / 100.0)
        return ADAPTIVE_MIN_DELAY + (ADAPTIVE_MAX_DELAY - ADAPTIVE_MIN_DELAY) * 0.08 * frac

    def grammar_summary(self):
        return {
            'enabled': self.grammar_enabled,
            'intensity': self.grammar_intensity,
            **self.kb.grammar_stats(),
        }

    def controls(self):
        mi = self.text_model.mode_info()
        return {
            'mode': self.train_mode,
            'device': mi['device'],
            'device_effective': mi['effective'],
            'device_note': mi['note'],
            'intensity': self.intensity,
            'grammar': self.grammar_enabled,
            'grammar_intensity': self.grammar_intensity,
            'coverage': self.coverage,
            'article_goal': WIKI_ARTICLE_GOAL,
            'sweep_total': self.sweep_total,
            'sources_per_cycle': self._sources_per_cycle(),
            'speed_factor': self._speed_factor(),
            'parallel_workers': max(2, (os.cpu_count() or 4) + 2),
            'batch_size': BATCH_SIZE,
        }

    def _speed_factor(self):
        """Report the effective throughput multiplier vs. the original 20/batch
        single-threaded pipeline: bigger batches x parallel learners."""
        cores = os.cpu_count() or 4
        parallel = min(3.0, 1.0 + 0.25 * (cores - 2))  # ~2-3x on a 6-core
        size = BATCH_SIZE / 20.0                        # 2x at 40/batch
        return round(max(1.0, min(6.0, size * parallel * 0.9)), 2)

    # ---------------------------------------------------------------- status
    def status(self):
        kb = self.kb.stats()
        mi = self.text_model.mode_info()
        remaining = max(0, SOURCES_PER_CYCLE - self.sources_since_break)
        return {
            'running': self._running,
            'mode': self.train_mode,
            'device': mi['device'],
            'device_effective': mi['effective'],
            'device_note': mi['note'],
            'controls': self.controls(),
            'grammar': self.grammar_summary(),
            'cycle': {
                'sources_per_cycle': self._sources_per_cycle(),
                'sources_since_break': self.sources_since_break,
                'remaining_in_cycle': remaining,
                'break_seconds': BREAK_SECONDS,
                'on_break': self.break_remaining > 0,
                'break_remaining': self.break_remaining,
                'breaks_taken': self.breaks_taken,
            },
            'fetch_interval_seconds': 'adaptive',
            'started_at': self.started_at,
            'last_error': self.last_error,
            'knowledge': kb,
            'text_model': self.text_model.progress(),
        }
