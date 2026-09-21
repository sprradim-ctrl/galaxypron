import os
import shutil
import threading
import time
from pathlib import Path


class TierStore:
    """SSD-cache-first + HDD mirror tiering.

    Writers always target the SSD cache. Readers read the SSD copy and only
    fall back to the HDD mirror if the SSD copy is missing. The HDD is kept
    in sync in the background (copy when the SSD copy is newer or absent).
    All a no-op (pure SSD) when no HDD dir is available.
    """

    def __init__(self, ssd_dir, hdd_dir=None):
        self.ssd = Path(ssd_dir)
        self.hdd = Path(hdd_dir) if hdd_dir else None
        self.enabled = hdd_dir is not None and Path(hdd_dir) != self.ssd
        self._lock = threading.Lock()
        self._worker = None
        self._pending = set()
        if self.enabled:
            try:
                self.hdd.mkdir(parents=True, exist_ok=True)
            except Exception:
                self.enabled = False
                self.hdd = None

    def cache(self, name):
        return self.ssd / name

    def read(self, name):
        p = self.ssd / name
        if p.exists():
            return p
        if self.enabled:
            hp = self.hdd / name
            if hp.exists():
                return hp
        return p

    def _copy_if_needed(self, name):
        if not self.enabled:
            return
        src = self.ssd / name
        dst = self.hdd / name
        try:
            if not src.exists():
                return
            need = True
            if dst.exists():
                ss, ds = src.stat(), dst.stat()
                need = int(ss.st_mtime) > int(ds.st_mtime) or ss.st_size != ds.st_size
            if need:
                tmp = dst.with_suffix(dst.suffix + '.tier.tmp')
                tmp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
        except Exception:
            pass

    def schedule(self, name):
        if not self.enabled:
            return
        with self._lock:
            self._pending.add(name)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()

    def mirror_all_now(self, names):
        if not self.enabled:
            return
        for n in names:
            self._copy_if_needed(n)

    def _run(self):
        while True:
            with self._lock:
                jobs = list(self._pending)
                self._pending.clear()
            if not jobs:
                return
            for n in jobs:
                self._copy_if_needed(n)
            time.sleep(1)