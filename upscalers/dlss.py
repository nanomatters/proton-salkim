"""Provision the pinned NVIDIA DLSS libraries; Wine selects replacements."""

import json
from pathlib import Path
import re
from urllib.error import URLError
from urllib.parse import urlparse

from .storage import ComponentError, directory_override, prepare_libraries


_NAMES = frozenset(("nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll"))


def catalog():
    data = json.loads((Path(__file__).parent / "releases/dlss.json").read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or
            not isinstance(data.get("version"), str) or
            not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", data["version"]) or
            not isinstance(data.get("files"), dict) or set(data["files"]) != _NAMES):
        raise ComponentError("Invalid DLSS manifest")
    for name, item in data["files"].items():
        if not isinstance(item, dict):
            raise ComponentError("Invalid DLSS file")
        url = urlparse(item.get("url", ""))
        if (url.scheme != "https" or url.hostname != "raw.githubusercontent.com" or
                not re.fullmatch(r"/NVIDIA/DLSS/[0-9a-f]{40}/lib/Windows_x86_64/rel/" + re.escape(name), url.path) or
                type(item.get("size")) is not int or item["size"] <= 0 or
                not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))):
            raise ComponentError("Invalid DLSS file")
    return data


def prepare(env, progress=None, *, log=print):
    override = directory_override(env, "WINE_DLSS_PROVIDER")
    if override:
        return override
    return prepare_libraries(env, "dlss", catalog(), progress, log=log)


def setup(env, *, gpu_eligible, progress=None, log=print):
    if env.get("WINE_OPTISCALER_NAME"):
        return False
    try:
        if not env.get("WINE_DLSS_PROVIDER") and (
                env.get("PROTON_UPSCALER_UPGRADE") != "1" or
                not (gpu_eligible() if callable(gpu_eligible) else gpu_eligible)):
            return False
        directory = prepare(env, progress, log=log)
        env["WINE_DLSS_PROVIDER"] = str(directory)
        log("NVIDIA DLSS ready: " + str(directory))
        return True
    except (ComponentError, OSError, ValueError, TypeError, URLError) as error:
        log("DLSS upgrade unavailable: " + str(error))
        return False
