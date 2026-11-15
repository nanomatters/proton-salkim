"""Optional component download progress using Proton's bundled Zenity executable."""

import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]


def _zenity_binary():
    bundled = ROOT / "protonfixes/files/bin/zenity"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return None


class Progress:
    """Optional progress UI; cancelling skips downloads for this launch."""

    _cancelled = False

    def __init__(self, log=None, *, component="AMD FSR"):
        self.component = component
        self.log = log
        self.process = None
        self._last_update = 0.0
        self._stage = None
        self._percentage = 0
        self._closed = False
        self._failed = False

    def _start(self):
        if self._closed or self._failed or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        if self.process is None:
            try:
                program = _zenity_binary()
                if not program:
                    raise FileNotFoundError("zenity was not found")
                self.process = subprocess.Popen(
                    [program, "--progress", "--percentage=0", "--cancel-label=Skip updates", "--auto-close",
                     "--title=Proton - " + self.component, "--text=Preparing " + self.component + " components...",
                     "--width=340"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                os.set_blocking(self.process.stdin.fileno(), False)
            except (OSError, ValueError) as error:
                self._failed = True
                if self.log is not None:
                    self.log(self.component + " progress window unavailable: %s" % error)
                self.close()
                return False
        status = self.process.poll()
        if status is not None:
            if status == 1:
                Progress._cancelled = True
            self.close()
            return False
        return True

    def update(self, stage, done=0, total=0):
        if self.process is not None and self.process.poll() == 1:
            Progress._cancelled = True
        if Progress._cancelled:
            self.close()
            return False
        now = time.monotonic()
        if stage == self._stage and now - self._last_update < 0.1 and done != total:
            return
        self._stage, self._last_update = stage, now
        if not self._start():
            return False if Progress._cancelled else None
        fraction = max(0, min(1, done / total)) if total > 0 else 0
        key = str(stage).lower()
        if key.startswith("extract"):
            percentage = 90 + int(9 * fraction)
            text = "Extracting " + self.component + " components..."
        elif key.startswith("download"):
            percentage = int(90 * fraction)
            text = "Updating " + self.component + " components..."
        else:
            percentage = self._percentage
            text = str(stage).replace("\n", " ").replace("\r", " ")[:256]
        self._percentage = max(self._percentage, percentage)
        try:
            os.write(self.process.stdin.fileno(), ("# %s\n%d\n" % (text, self._percentage)).encode("utf-8"))
        except BlockingIOError:
            pass
        except (OSError, ValueError):
            self.close()
            return False if Progress._cancelled else None

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.process is None:
            return
        if self.process.stdin is not None:
            try:
                if self.process.poll() is None:
                    os.write(self.process.stdin.fileno(), b"100\n")
                self.process.stdin.close()
            except (OSError, ValueError):
                try:
                    self.process.stdin.close()
                except (OSError, ValueError):
                    pass
        status = None
        try:
            status = self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                self.process.terminate()
            except OSError:
                pass
            try:
                status = self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    status = self.process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if status == 1:
            Progress._cancelled = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
