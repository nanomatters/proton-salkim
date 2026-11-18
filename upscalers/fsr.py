"""Select a shared AMD FSR provider independently of the legacy downloader."""

import json
from pathlib import Path
import re
import subprocess
from urllib.error import URLError

from .storage import ComponentError, Store, archive_ranges, data_home, protect_managed, verified_files


FALSE_VALUES = {"", "0", "false", "no", "off"}


def catalog():
    data = json.loads((Path(__file__).parent / "releases/fsr.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ComponentError("Unsupported FSR release catalog")
    releases = data.get("releases", {})
    if not isinstance(releases, dict) or data.get("default") not in releases:
        raise ComponentError("Invalid FSR release catalog")
    for version, release in releases.items():
        if (not isinstance(release, dict) or
                not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version) or release.get("version") != version):
            raise ComponentError("Invalid FSR release identifier")
        for key in ("archive", "provider"):
            entry = release.get(key, {})
            if (not isinstance(entry, dict) or
                    not isinstance(entry.get("size"), int) or entry["size"] <= 0 or
                    not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))):
                raise ComponentError("Invalid FSR release checksum or size")
        archive_ranges(release["archive"])
    return data


def _activate(env, provider):
    env["WINE_FSR4_PROVIDER"] = str(provider)
    requested = env.get("PROTON_MLFG_UPGRADE")
    if requested is not None:
        env.setdefault("MLFG_UPGRADE", "0" if requested.strip().lower() in FALSE_VALUES else "1")


def _provider_override(env, value):
    """Keep saved paths working after their managed release has moved."""
    path = Path(value)
    root = data_home(env) / "proton/components/fsr"
    legacy = root / "driver"
    if path.name != "amdxcffx64.dll" or legacy.is_symlink() or path.exists():
        return value
    try:
        if path.parent.parent.resolve() != legacy.resolve():
            return value
        directory = root / path.parent.name
        if directory.is_symlink():
            return value
        manifest = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        provider = manifest["provider"]
        candidate = directory / path.name
        if manifest["version"] == directory.name and verified_files(directory, {path.name: provider}):
            return str(candidate.resolve())
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return value


def setup(env, *, gpu_eligible, progress=None, log=print):
    """Prepare the provider selected by the bundled manifest."""
    try:
        if env.get("WINE_FSR4_PROVIDER"):
            provider = _provider_override(env, env["WINE_FSR4_PROVIDER"])
            protect_managed(env, "fsr", provider)
            _activate(env, provider)
            return True
        if env.get("PROTON_UPSCALER_UPGRADE") != "1" and not env.get("WINE_OPTISCALER_NAME"):
            return False
        if not (gpu_eligible() if callable(gpu_eligible) else gpu_eligible):
            return False
        data = catalog()
        release = data["releases"][data["default"]]
        store = Store(env)
        try:
            with store.lock():
                provider = store.prepare(release, progress)
        except (ComponentError, OSError, ValueError, subprocess.SubprocessError) as error:
            directory = store.fallback(release, error, log)
            if directory is None:
                raise
            provider = directory / "amdxcffx64.dll"
        _activate(env, provider.resolve())
        log("AMD FSR provider ready: " + str(provider))
        return True
    except (ComponentError, OSError, ValueError, URLError, subprocess.SubprocessError) as error:
        log("FSR upgrade unavailable: " + str(error))
        return False
