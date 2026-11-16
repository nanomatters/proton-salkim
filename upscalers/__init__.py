"""Independent upscaler component setup for the Proton launcher."""

import platform

from . import dlss, ffx, fsr, progress, xess


_HELPERS = frozenset((
    "iscriptevaluator.exe", "wineboot.exe", "winecfg.exe", "regedit.exe",
    "reg.exe", "rundll32.exe", "explorer.exe", "services.exe", "winedbg.exe",
))


def eligible_amd(gpus):
    """Use Vulkan capabilities for provisioning; Wine checks the actual device."""
    gpus = tuple(gpus)
    # Use an iGPU automatically only when no discrete or unknown GPU is visible.
    allow_integrated = all(
        getattr(getattr(gpu, "deviceType", None), "name", "") in ("INTEGRATED_GPU", "CPU")
        for gpu in gpus)
    for gpu in gpus:
        kind = getattr(getattr(gpu, "deviceType", None), "name", "")
        if gpu.vendorID != 0x1002 or kind not in (
                ("DISCRETE_GPU", "INTEGRATED_GPU") if allow_integrated else ("DISCRETE_GPU",)):
            continue
        names = {getattr(extension, "extensionName", extension) for extension in gpu.extensions}
        if "VK_KHR_fragment_shading_rate" in names or "VK_EXT_shader_float8" in names:
            return True
    return False


def default_amd_lite(gpus):
    """Use Lite for compatible iGPU-only systems without native FP8 support."""
    devices = [gpu for gpu in gpus if getattr(getattr(gpu, "deviceType", None), "name", "") != "CPU"]
    if not devices or any(getattr(getattr(gpu, "deviceType", None), "name", "") != "INTEGRATED_GPU"
                          for gpu in devices):
        return False
    amd = [gpu for gpu in devices if gpu.vendorID == 0x1002]
    return bool(amd) and all(
        "VK_KHR_fragment_shading_rate" in {getattr(ext, "extensionName", ext) for ext in gpu.extensions} and
        "VK_EXT_shader_float8" not in {getattr(ext, "extensionName", ext) for ext in gpu.extensions}
        for gpu in amd)


def eligible_nvidia(gpus):
    for gpu in gpus:
        if gpu.vendorID != 0x10de or getattr(getattr(gpu, "deviceType", None), "name", "") not in (
                "DISCRETE_GPU", "INTEGRATED_GPU"):
            continue
        names = {getattr(extension, "extensionName", extension) for extension in gpu.extensions}
        if names.intersection(("VK_KHR_cooperative_matrix", "VK_NV_cooperative_matrix")):
            return True
    return False


def eligible_xess(gpus):
    for gpu in gpus:
        if getattr(getattr(gpu, "deviceType", None), "name", "") not in ("DISCRETE_GPU", "INTEGRATED_GPU"):
            continue
        names = {getattr(extension, "extensionName", extension) for extension in gpu.extensions}
        if ("shaderIntegerDotProduct" in getattr(gpu, "features", ()) or
                "VK_KHR_shader_integer_dot_product" in names):
            return True
    return False


def setup(env, *, mode, command, gpu_query, log=print):
    if mode not in ("run", "waitforexitandrun") or not command:
        return False
    executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable in _HELPERS:
        return False
    if env.get("WINEARCH") == "win32" or platform.machine() not in ("x86_64", "amd64"):
        return False
    enabled = env.get("PROTON_UPSCALER_UPGRADE") == "1"
    explicit_fsr = bool(env.get("WINE_FSR4_PROVIDER"))
    if not enabled and not any(env.get(key) for key in (
            "WINE_FSR4_PROVIDER", "WINE_FFX_PROVIDER", "WINE_DLSS_PROVIDER", "WINE_XESS_PROVIDER")):
        return False
    devices = None
    provider_ready = False

    def gpus():
        nonlocal devices
        if devices is None:
            devices = tuple(gpu_query())
        return devices

    try:
        with progress.Progress(log=log) as reporter:
            provider_ready = fsr.setup(env, gpu_eligible=lambda: eligible_amd(gpus()),
                                       progress=reporter.update, log=log)
        if provider_ready and not explicit_fsr and "WINE_FSR4_LITE" not in env and default_amd_lite(gpus()):
            env["WINE_FSR4_LITE"] = "1"
        if (provider_ready and enabled) or env.get("WINE_FFX_PROVIDER"):
            with progress.Progress(log=log) as reporter:
                ffx.setup(env, progress=reporter.update, log=log)
        with progress.Progress(log=log, component="NVIDIA DLSS") as reporter:
            dlss_ready = dlss.setup(env, gpu_eligible=lambda: eligible_nvidia(gpus()),
                                   progress=reporter.update, log=log)
        with progress.Progress(log=log, component="Intel XeSS") as reporter:
            xess_ready = xess.setup(env, gpu_eligible=lambda: eligible_xess(gpus()),
                                   progress=reporter.update, log=log)
        return provider_ready or dlss_ready or xess_ready
    except Exception as error:
        log("Upscaler component setup failed; keeping the current game configuration: " + str(error))
        return provider_ready
