"""System 7-Zip selection and a verified, shared fallback tool."""

import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.request


VERSION = "26.03"
BUILDS = {
    "x86_64": ("x64", "dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695"),
    "aarch64": ("arm64", "2389ba20e4d8295e8709c20b6263b69bd1ec4972fe38a04ad7a1badbf595b996"),
}
MEMBERS = ("7zzs", "License.txt", "readme.txt", "MANUAL/general/license.htm")


def _checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _download(url, path, checksum, progress=None):
    from .storage import ComponentError, report, response_chunks

    if path.is_file() and not path.is_symlink() and _checksum(path) == checksum:
        return
    request = urllib.request.Request(url, headers={"User-Agent": "Proton-components"})
    report(progress, "download", 0, 0)
    with tempfile.TemporaryFile(dir=path.parent) as output:
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(request, timeout=15) as response:
            for block in response_chunks(response):
                size += len(block)
                if size > 64 * 1024 * 1024:
                    raise ComponentError("7-Zip archive exceeds its size limit")
                digest.update(block)
                output.write(block)
                report(progress, "download", size, 0)
        if digest.hexdigest() != checksum:
            raise ValueError("7-Zip download does not match its SHA-256 checksum")
        output.seek(0)
        _write(path, output.read(), 0o600)


def extractor(cache=None, *, env=None, progress=None):
    """Prefer system 7-Zip; otherwise reuse or fetch the official static binary."""
    from .storage import locked

    env = os.environ if env is None else env
    for name in ("7zz", "7z", "7zzs"):
        program = shutil.which(name, path=env.get("PATH", os.defpath))
        if program:
            return Path(program)
    machine = platform.machine().lower()
    if machine not in BUILDS:
        raise ValueError("No bundled 7-Zip build for " + machine)
    arch, checksum = BUILDS[machine]
    if cache is None:
        cache_home = env.get("XDG_CACHE_HOME", "")
        if not cache_home or not Path(cache_home).is_absolute():
            cache_home = Path(env.get("HOME") or Path.home()) / ".cache"
        # Keep the existing OptiScaler tool cache shared with FSR.
        cache = Path(cache_home) / "protonfixes/upscalers"
    directory = Path(cache) / "7zip" / VERSION / arch
    directory.mkdir(parents=True, exist_ok=True)
    with locked(directory / ".lock"):
        name = "7z" + VERSION.replace(".", "") + "-linux-" + arch + ".tar.xz"
        archive = directory / name
        _download("https://github.com/ip7z/7zip/releases/download/" + VERSION + "/" + name,
                  archive, checksum, progress)
        try:
            with tarfile.open(archive, "r:xz") as source:
                for name in MEMBERS:
                    member = source.getmember(name)
                    if not member.isfile():
                        raise ValueError("Unexpected 7-Zip member: " + name)
                    with source.extractfile(member) as stream:
                        data = stream.read()
                    target = directory / name
                    mode = 0o700 if name == "7zzs" else 0o600
                    if (target.is_symlink() or not target.is_file() or target.read_bytes() != data or
                            stat.S_IMODE(target.stat().st_mode) != mode):
                        _write(target, data, mode)
        except (tarfile.TarError, KeyError, EOFError) as error:
            raise ValueError("Invalid 7-Zip archive: " + str(error)) from error
    return directory / "7zzs"
