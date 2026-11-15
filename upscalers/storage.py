"""Download and extract pinned components without running their installers."""

from contextlib import contextmanager
import fcntl
import errno
import hashlib
import json
import os
import re
import selectors
import shutil
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlparse

from . import sevenzip


class ComponentError(Exception):
    """A component could not be prepared; the game can use its own files."""


class _RangeUnsupported(ComponentError):
    """The server requires a complete download."""


def data_home(env):
    value = env.get("XDG_DATA_HOME", "")
    return Path(value) if value and os.path.isabs(value) else Path.home() / ".local/share"


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ComponentError("Another component update is in progress") from error
        cleanup_preparations(path.parent)
        yield


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def matches(path, size, digest):
    path = Path(path)
    return (not path.is_symlink() and path.is_file() and
            path.stat().st_size == size and checksum(path) == digest)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verified_files(directory, files):
    """Check every component file against its expected checksum."""
    directory = Path(directory)
    if directory.is_symlink():
        return False
    try:
        return all(matches(directory / name, item["size"], item["sha256"])
                   for name, item in files.items())
    except OSError:
        return False


def cleanup_preparations(root):
    """Called under the component lock; recover interrupted publication first."""
    root = Path(root)
    for directory in root.glob(".prepare-*"):
        if directory.is_symlink():
            directory.unlink()
            continue
        if not directory.is_dir():
            continue
        previous = directory / "previous"
        if previous.exists() or previous.is_symlink():
            if previous.is_symlink():
                continue
            try:
                journal = directory / "restore.json"
                if journal.is_symlink():
                    continue
                if journal.exists():
                    version = json.loads(journal.read_text(encoding="utf-8"))["target"]
                else:
                    # Earlier builds left the old release here without a journal.
                    metadata = previous / "metadata.json"
                    if metadata.is_symlink():
                        continue
                    version = json.loads(metadata.read_text(encoding="utf-8"))["version"]
                if (previous.is_symlink() or not isinstance(version, str) or
                        not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", version)):
                    continue
                target = root / version
                if target.is_symlink():
                    continue
                if not target.exists():
                    os.replace(previous, target)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        shutil.rmtree(directory)


def publish(directory, target):
    """Publish with an atomic final rename and a recoverable previous release."""
    directory, target = Path(directory), Path(target)
    work = Path(tempfile.mkdtemp(prefix=".prepare-", dir=target.parent))
    previous = work / "previous"
    try:
        stage = directory
        if directory.stat().st_dev != target.parent.stat().st_dev:
            stage = work / directory.name
            shutil.copytree(directory, stage)
        atomic_json(work / "restore.json", {"target": target.name})
        if target.exists():
            os.replace(target, previous)
        try:
            os.replace(stage, target)
        except OSError:
            if previous.exists():
                os.replace(previous, target)
            raise
    finally:
        # A failed rollback must survive for recovery on the next launch.
        if not previous.exists() or target.exists():
            shutil.rmtree(work)


def _boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return value if re.fullmatch(r"[0-9a-f-]{36}", value) else None
    except (OSError, ValueError):
        return None


RETENTION_LOCK_TIMEOUT = 2.0


@contextmanager
def _retention_guard(root):
    try:
        lock = (Path(root) / ".retention.lock").open("a+b")
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise ComponentError("Cannot protect the component cache") from error
        yield False  # An unwritable cache remains usable, with pruning disabled.
        return
    with lock:
        deadline = time.monotonic() + RETENTION_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ComponentError("Component cache cleanup is in progress") from error
                time.sleep(min(.01, remaining))
        yield True


def _managed_releases(root, *, provider_only=False):
    releases = []
    for path in root.glob("*/metadata.json"):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != path.parent.name or
                    not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", path.parent.name)):
                continue
            if provider_only and ("provider" not in data or "files" in data):
                continue
            files = _cache_files(data)
            if (not isinstance(files, dict) or not files or
                    not all(isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name) and
                            isinstance(item, dict) and type(item.get("size")) is int and item["size"] > 0 and
                            isinstance(item.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                            for name, item in files.items())):
                continue
            releases.append((path.stat().st_mtime_ns, path.parent.name))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return [name for _, name in sorted(releases, reverse=True)]


def _retain_selected(root, manifest, *, prune=True):
    """Called under the retention lock; current-boot users survive until reboot."""
    boot_id = _boot_id()
    if boot_id is None:
        return
    try:
        names = _managed_releases(root)
        path = root / ".retention.json"
        state = json.loads(path.read_text(encoding="utf-8")) if not path.is_symlink() else None
        if (not isinstance(state, dict) or not isinstance(state.get("boot_id"), str) or
                not re.fullmatch(r"[0-9a-f-]{36}", state["boot_id"]) or
                not all(isinstance(state.get(key), list) and
                        all(isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name)
                            for name in state[key]) for key in ("used", "recent"))):
            state = None
    except (OSError, ValueError):
        names, state = _managed_releases(root), None
    recent = state["recent"] if state else names
    used = state["used"] if state and state["boot_id"] == boot_id else []
    used = set(used) | (set(names) - set(recent)) | {manifest["version"]}
    if state is None:
        used.update(names)  # Older builds may already be using these releases.
    recent = list(dict.fromkeys([manifest["version"], *recent, *names]))
    recent = [name for name in recent if name in names]
    keep = set(recent[:3]) | used
    try:
        keep.add(json.loads((root / ".last-good.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    updated = {"boot_id": boot_id, "used": sorted(used & set(names)), "recent": recent}
    remove = set(names) - keep if prune else set()
    try:
        if updated != state or remove:
            atomic_json(path, updated)  # Never prune if usage cannot be recorded.
        for name in remove:
            directory = root / name
            if not directory.is_symlink():
                shutil.rmtree(directory)
    except OSError:
        pass


def protect_managed(env, component, path):
    """Pin explicit overrides only when they name a managed cached release."""
    root = data_home(env) / "proton/components" / component
    directory = Path(path)
    if component == "fsr":
        directory = directory.parent
        legacy = root / "driver"
        if not legacy.is_symlink() and directory.resolve().parent == legacy.resolve():
            root = legacy
    try:
        root, directory = root.resolve(), directory.resolve()
        if directory.parent != root:
            return
        metadata = directory / "metadata.json"
        if metadata.is_symlink():
            return
        manifest = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != directory.name:
            return
        with _retention_guard(root) as writable:
            if writable:
                _retain_selected(root, manifest, prune=False)
    except (ComponentError, OSError, ValueError, KeyError, TypeError):
        return


def _remember(root, manifest):
    """Remember a verified release without making cache writes mandatory."""
    with _retention_guard(root) as writable:
        path = root / ".last-good.json"
        try:
            if path.is_symlink() or not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != manifest:
                atomic_json(path, manifest)
        except (OSError, ValueError):
            pass
        if writable:
            _retain_selected(root, manifest)


def _cache_files(manifest):
    return manifest["files"] if "files" in manifest else {"amdxcffx64.dll": manifest["provider"]}


def cached_fallback(root, requested, error, log=print):
    """Use a complete previously verified release when an update is unavailable."""
    root = Path(root)
    try:
        with _retention_guard(root) as writable:
            return _cached_fallback(root, requested, error, log, writable)
    except ComponentError:
        return None


def _cached_fallback(root, requested, error, log, writable):
    expected = set(_cache_files(requested))
    try:
        candidates = sorted((path for path in root.glob("*/metadata.json")
                             if not path.parent.is_symlink() and not path.is_symlink()),
                            key=lambda path: path.stat().st_mtime_ns, reverse=True)
    except OSError:
        candidates = []
    # Existing installations predate the marker; their saved manifests bootstrap it.
    for path in [root / ".last-good.json", *candidates]:
        try:
            if path.is_symlink():
                continue
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                continue
            version = manifest.get("version")
            if not isinstance(version, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", version):
                continue
            directory = root / version
            if directory.is_symlink() or not directory.is_dir():
                continue
            if path.name == "metadata.json" and directory != path.parent:
                continue
            if "files" in requested and manifest.get("schema") != requested["schema"]:
                continue
            if "provider" in requested and manifest.get("architecture") != requested.get("architecture"):
                continue
            files = _cache_files(manifest)
            if not isinstance(files, dict) or set(files) != expected:
                continue
            if not all(isinstance(item, dict) and type(item.get("size")) is int and item["size"] > 0 and
                       isinstance(item.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                       for name, item in files.items()):
                continue
            if not verified_files(directory, files):
                continue
            if writable:
                _retain_selected(root, manifest, prune=False)
            log("Component update unavailable (%s); using verified cached release %s" % (error, directory))
            return directory.resolve()
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None






def _metadata(release):
    provider = {key: value for key, value in release["provider"].items()
                if key != "effect_version" and not key.startswith("model_")}
    return {**release, "provider": provider}


def report(progress, stage, done, total):
    if progress is not None and progress(stage, done, total) is False:
        raise ComponentError("Component preparation cancelled")


DOWNLOAD_PROGRESS_WINDOW = 60
MIN_DOWNLOAD_PROGRESS = 1024
EXTRACT_TIMEOUT = 3 * 60


def response_chunks(response):
    """Allow slow transfers while rejecting connections that barely progress."""
    start, received = time.monotonic(), 0
    while True:
        # read1 returns after one underlying read, even on a trickling connection.
        block = response.read1(1024 * 1024)
        if not block:
            return
        received += len(block)
        now = time.monotonic()
        if now - start >= DOWNLOAD_PROGRESS_WINDOW:
            if received < MIN_DOWNLOAD_PROGRESS:
                raise ComponentError("Component download stalled")
            start, received = now, 0
        yield block


def download_file(item, destination, progress=None, *, hosts, referer=None, byte_range=None):
    address = urlparse(item["url"])
    if address.scheme != "https" or address.hostname not in hosts:
        raise ComponentError("Unrecognised component download source")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Proton component manager)",
               "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    if byte_range is not None:
        offset, total = byte_range
        end = offset + item["size"] - 1
        headers["Range"] = f"bytes={offset}-{end}"
    request = urllib.request.Request(item["url"], headers=headers)
    digest = hashlib.sha256()
    received = 0
    report(progress, "download", 0, item["size"])
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except HTTPError as error:
        if byte_range is not None and error.code in (400, 405, 416, 501):
            error.close()
            raise _RangeUnsupported("Partial downloads are unavailable") from error
        raise
    with response:
        address = urlparse(response.geturl())
        if address.scheme != "https" or address.hostname not in hosts:
            raise ComponentError("Unrecognised component download source")
        if byte_range is not None:
            if response.status == 200:
                raise _RangeUnsupported("The server ignored the download range")
            if (response.status != 206 or
                    response.headers.get("Content-Range") != f"bytes {offset}-{end}/{total}" or
                    response.headers.get("Content-Encoding", "identity").strip().lower() != "identity" or
                    response.headers.get("Content-Length", str(item["size"])) != str(item["size"])):
                raise ComponentError("Invalid partial component response")
        with Path(destination).open("r+b" if byte_range is not None else "wb") as output:
            if byte_range is not None:
                output.seek(offset)
            for block in response_chunks(response):
                received += len(block)
                if received > item["size"]:
                    raise ComponentError("Component download exceeds its recorded size")
                output.write(block)
                digest.update(block)
                report(progress, "download", received, item["size"])
    if received != item["size"] or digest.hexdigest() != item["sha256"]:
        raise ComponentError("Component download does not match the selected release")


def archive_ranges(archive):
    """Validate optional byte ranges pinned to this driver release."""
    ranges = archive.get("ranges")
    if ranges is None:
        return None
    if not isinstance(ranges, list) or not ranges:
        raise ComponentError("Invalid driver download ranges")
    previous_end = 0
    total = 0
    for part in ranges:
        if (not isinstance(part, dict) or type(part.get("offset")) is not int or
                type(part.get("size")) is not int or part["offset"] < previous_end or
                part["size"] <= 0 or part["offset"] + part["size"] > archive["size"] or
                not isinstance(part.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", part["sha256"])):
            raise ComponentError("Invalid driver download ranges")
        previous_end = part["offset"] + part["size"]
        total += part["size"]
    if total >= archive["size"]:
        raise ComponentError("Driver download ranges do not reduce the download")
    return ranges


def download(release, destination, progress=None):
    archive = release["archive"]
    options = {"hosts": ("drivers.amd.com", "download.amd.com"), "referer": release["release_notes"]}
    if ranges := archive_ranges(archive):
        total = sum(part["size"] for part in ranges)
        completed = 0
        report(progress, "download", 0, total)
        # Preserve archive offsets while leaving unused compressed blocks as holes.
        with Path(destination).open("wb") as output:
            output.truncate(archive["size"])
        try:
            for part in ranges:
                def update(stage, done, _total):
                    report(progress, stage, completed + done, total)
                download_file({**part, "url": archive["url"]}, destination, update,
                              byte_range=(part["offset"], archive["size"]), **options)
                completed += part["size"]
            return
        except _RangeUnsupported:
            pass
    download_file(archive, destination, progress, **options)


def extract(archive, release, destination, progress=None, *, program):
    payload = release["provider"]
    received = 0
    digest = hashlib.sha256()
    report(progress, "extract", 0, payload["size"])
    deadline = time.monotonic() + EXTRACT_TIMEOUT
    with tempfile.TemporaryFile(dir=Path(destination).parent) as errors:
        process = subprocess.Popen(
            [program, "e", "-so", "--", str(archive), payload["member"]],
            stdout=subprocess.PIPE, stderr=errors)
        try:
            with selectors.DefaultSelector() as selector, Path(destination).open("wb") as output:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ComponentError("Component extraction timed out")
                    report(progress, "extract", received, payload["size"])
                    if not selector.select(min(remaining, .2)):
                        continue
                    block = os.read(process.stdout.fileno(), 1024 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received > payload["size"]:
                        raise ComponentError("Extracted FSR provider exceeds its recorded size")
                    output.write(block)
                    digest.update(block)
                    report(progress, "extract", received, payload["size"])
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                errors.seek(0)
                detail = errors.read(4096).decode("utf-8", "replace").strip()
                raise ComponentError("Cannot extract the FSR provider: " + detail)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    if received != payload["size"] or digest.hexdigest() != payload["sha256"]:
        raise ComponentError("Extracted FSR provider does not match the selected release")


class Store:
    def __init__(self, env, *, root=None):
        self.env = env
        self.root = Path(root) if root is not None else data_home(env) / "proton/components/fsr"
        self.legacy = None if root is not None else self.root / "driver"

    def lock(self):
        return locked(self.root / ".lock")

    def _legacy_store(self):
        if self.legacy is not None and self.legacy.is_dir() and not self.legacy.is_symlink():
            return Store(self.env, root=self.legacy)
        return None

    def _migrate(self, release):
        """Called under the flat cache lock; old paths stay valid for this boot."""
        legacy = self._legacy_store()
        if legacy is None:
            return
        try:
            with legacy.lock(), _retention_guard(legacy.root) as writable:
                boot = _boot_id()
                if not writable or boot is None:
                    return
                path = legacy.root / ".retention.json"
                try:
                    state = json.loads(path.read_text(encoding="utf-8")) if not path.is_symlink() else None
                except (OSError, ValueError):
                    state = None
                names = _managed_releases(legacy.root, provider_only=True)
                if (not isinstance(state, dict) or not isinstance(state.get("boot_id"), str) or
                        not re.fullmatch(r"[0-9a-f-]{36}", state["boot_id"]) or
                        not all(isinstance(state.get(key), list) and
                                all(isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name)
                                    for name in state[key]) for key in ("used", "recent"))):
                    if names:
                        sample = json.loads((legacy.root / names[0] / "metadata.json").read_text(encoding="utf-8"))
                        _retain_selected(legacy.root, sample, prune=False)
                    return
                if state["boot_id"] == boot:
                    return
                with _retention_guard(self.root) as flat_writable:
                    if not flat_writable:
                        return
                    for version in names:
                        target = self.root / version
                        if not target.exists() and not target.is_symlink():
                            os.replace(legacy.root / version, target)
                    # Do not merge independently updated selection histories.
                    if not _managed_releases(legacy.root, provider_only=True):
                        for name in (".last-good.json", ".retention.json"):
                            source, target = legacy.root / name, self.root / name
                            if source.is_file() and not source.is_symlink() and not target.exists() and not target.is_symlink():
                                os.replace(source, target)
        except (ComponentError, OSError, ValueError, KeyError, TypeError):
            pass  # A read-only or busy legacy cache can still be reused below.

    def fallback(self, release, error, log=print):
        directory = cached_fallback(self.root, release, error, log)
        legacy = self._legacy_store()
        if directory is None and legacy is not None:
            directory = cached_fallback(legacy.root, release, error, log)
        return directory

    def installed(self, release):
        directory = self.root / release["version"]
        provider = directory / "amdxcffx64.dll"
        try:
            metadata_path = directory / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or not isinstance(metadata.get("provider"), dict):
                return None
            if (metadata.get("version") != release["version"] or
                    metadata.get("provider", {}).get("sha256") != release["provider"]["sha256"] or
                    not verified_files(directory, {"amdxcffx64.dll": release["provider"]})):
                return None
            cleaned = _metadata(metadata)
            if cleaned != metadata:
                try:
                    atomic_json(metadata_path, cleaned)
                except OSError:
                    pass
            return provider
        except (OSError, ValueError, TypeError):
            return None

    def prepare(self, release, progress=None, archive=None):
        """Called under lock; publish only a complete, verified installation."""
        self._migrate(release)
        target = self.root / release["version"]
        if target.is_symlink():
            raise ComponentError("The FSR component directory must not be a symlink")
        current = self.installed(release)
        if current:
            _remember(self.root, _metadata(release))
            return current
        legacy = self._legacy_store()
        if legacy is not None:
            with legacy.lock():
                previous = None if (legacy.root / release["version"]).is_symlink() else legacy.installed(release)
                if previous:
                    _remember(legacy.root, _metadata(release))
                    return previous
        program = sevenzip.extractor(env=self.env, progress=progress)
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.root) as name:
            temporary = Path(name)
            if archive is None:
                archive = temporary / "driver.exe"
                download(release, archive, progress)
            else:
                archive = Path(archive)
                if not matches(archive, release["archive"]["size"], release["archive"]["sha256"]):
                    raise ComponentError("The imported AMD driver does not match the selected release")
            prepared = temporary / "component"
            prepared.mkdir()
            extract(archive, release, prepared / "amdxcffx64.dll", progress, program=program)
            if not verified_files(prepared, {"amdxcffx64.dll": release["provider"]}):
                raise ComponentError("Extracted FSR provider failed verification")
            atomic_json(prepared / "metadata.json", _metadata(release))
            publish(prepared, target)
        _remember(self.root, _metadata(release))
        return target / "amdxcffx64.dll"
