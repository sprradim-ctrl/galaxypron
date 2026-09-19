"""galaxypron disc watcher.

Watches removable / optical drives for a valid galaxypron verification disc.
When the disc is inserted (or already present at launch, e.g. after a reboot)
and the app is not running, it launches the app once. Scans every 2 seconds.

Runs headless, so call it with pythonw.exe. A pid lock file keeps it to a
single instance so the scheduled logon task and the app never double-spawn.
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import time

VERIFY_FILENAME = 'galaxypron.verify'
VERIFY_TOKEN = 'GALAXYPRON-VERIFY-7A37B03C7A9942278E228F48'
LOCK_FILE = os.path.join(tempfile.gettempdir(), 'galaxypron_disk_watcher.pid')
POLL_SECONDS = 2

DRIVE_REMOVABLE = 2
DRIVE_CDROM = 5
CREATE_NO_WINDOW = 0x08000000


def candidate_drives():
    result = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if bitmask & (1 << i):
            letter = chr(ord('A') + i)
            drive = letter + ':\\'
            if ctypes.windll.kernel32.GetDriveTypeW(drive) in (DRIVE_REMOVABLE, DRIVE_CDROM):
                result.append(letter + ':')
    return result


def drive_has_verify(drive):
    try:
        with open(os.path.join(drive, VERIFY_FILENAME), 'r', encoding='utf-8',
                  errors='ignore') as f:
            return f.read().strip() == VERIFY_TOKEN
    except Exception:
        return False


def pid_alive(pid):
    try:
        out = subprocess.run(
            ['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW).stdout
        return str(pid) in out
    except Exception:
        return False


def acquire_single_instance():
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, 'r') as f:
                old = f.read().strip()
            if old.isdigit() and pid_alive(int(old)):
                return False
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True


def app_running(target):
    exe = os.path.basename(target).lower()
    try:
        out = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq %s' % exe, '/NH'],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW).stdout
        return exe in out.lower()
    except Exception:
        return True


def main():
    target = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == '--target' and i + 1 < len(args):
            target = args[i + 1]
    if not target:
        print('usage: disk_watcher.py --target <app exe>')
        return 2
    target = os.path.abspath(target)
    if not os.path.exists(target):
        print('target not found: %s' % target)
        return 2
    if not acquire_single_instance():
        return 0
    been_present = False
    while True:
        try:
            present = any(drive_has_verify(d) for d in candidate_drives())
            if present:
                if not been_present:
                    been_present = True
                    if not app_running(target):
                        subprocess.Popen([target], cwd=os.path.dirname(target))
            else:
                been_present = False
        except Exception:
            pass
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    sys.exit(main())