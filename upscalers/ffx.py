"""Provision the pinned FidelityFX SDK; Wine selects compatible replacements."""

import json
from pathlib import Path
import re
from urllib.error import URLError
from urllib.parse import urlparse

from .storage import ComponentError, directory_override, prepare_libraries


_NAMES = frozenset((
    "amd_fidelityfx_dx12.dll", "amd_fidelityfx_vk.dll",
    "amd_fidelityfx_loader_dx12.dll", "amd_fidelityfx_upscaler_dx12.dll",
    "amd_fidelityfx_framegeneration_dx12.dll", "amd_fidelityfx_denoiser_dx12.dll",
    "amd_fidelityfx_radiancecache_dx12.dll", "amd_fidelityfx_loader_vk.dll",
    "amd_fidelityfx_upscaler_vk.dll", "amd_fidelityfx_framegeneration_vk.dll",
))


def catalog():
    data = json.loads((Path(__file__).parent / "releases/ffx.json").read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", data.get("version", "")) or
            set(data.get("files", {})) != _NAMES):
        raise ComponentError("Invalid FidelityFX SDK manifest")
    for name, item in data["files"].items():
        if not isinstance(item, dict):
            raise ComponentError("Invalid FidelityFX SDK file")
        url = urlparse(item.get("url", ""))
        if (url.scheme != "https" or url.hostname != "raw.githubusercontent.com" or
                not re.fullmatch(r"/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/[0-9a-f]{40}/.+/" + re.escape(name), url.path) or
                not isinstance(item.get("size"), int) or item["size"] <= 0 or
                not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))):
            raise ComponentError("Invalid FidelityFX SDK file")
    return data


def prepare(env, progress=None, *, log=print):
    """Return one complete SDK directory, shared by games and OptiScaler."""
    override = directory_override(env, "WINE_FFX_PROVIDER")
    if override:
        return override
    if env.get("PROTON_UPSCALER_UPGRADE") != "1":
        raise ComponentError("Automatic component upgrades are disabled")
    return prepare_libraries(env, "ffx", catalog(), progress, log=log)


def setup(env, *, progress=None, log=print):
    if env.get("WINE_OPTISCALER_NAME"):
        return False
    try:
        directory = prepare(env, progress, log=log)
        env["WINE_FFX_PROVIDER"] = str(directory)
        log("FidelityFX SDK ready: " + str(directory))
        return True
    except (ComponentError, OSError, ValueError, TypeError, URLError) as error:
        log("FidelityFX SDK upgrade unavailable: " + str(error))
        return False
