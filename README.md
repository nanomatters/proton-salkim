![Proton Wineland](wineland-banner.png)

What is Proton Wineland?
-------------------------

Proton Wineland (aka Salkim) is a Proton distribution for running Windows
games and applications on Linux. It develops fixes across Wine, DXVK,
VKD3D-Proton, Mesa/RADV, and the Proton runtime, covering compatibility,
rendering, audio, input, and performance.

Proton Wineland aims to solve Linux gaming problems through complete solutions
rather than accumulating hacks and workarounds for individual games. Its goal
is to address issues that are genuinely solvable when the necessary time and
care are invested.

The project started with native Wayland support. That remains an important
part of the work, but its scope now includes many other improvements. When
a problem crosses component boundaries, the work follows it into the relevant
parts of the stack, including the Linux kernel where necessary.

Improvements are tested in the workloads that exposed them. Their benefit
elsewhere depends on the application and system.


Which one is the correct name, Wineland or Salkim?
--------------------------------------------------

Both are valid right now. There are two reasons for considering a rename.
First, people often confuse Wineland with Wayland. Knowing that Wineland
originated from Proton-CachyOS, some report Wayland-related issues to the
CachyOS team even though they are using Wineland. I have also seen websites
refer to Wayland as Wineland when discussing other Proton distributions,
such as CachyOS or GE, that use Wayland work from Wineland/Salkim.

Second, the project started with Wayland improvements in Wine but has grown
far beyond that. Salkim, which means "bunch" in Turkish, as in "a bunch of
grapes", seems to be a more suitable name.

There has also been some pushback against the rename. Some people prefer
Wineland because they think it sounds better. :)

For now, I haven't made a complete switch. I use the Salkim suffix for each
modified component. The Proton distribution is still called Wineland, while
I use Salkim for the project as a whole.


What does Proton Wineland offer?
--------------------------------

- **Wayland desktops and launchers.** Native Wayland support is enabled by
  default, with work on Chromium/CEF launchers, fullscreen and borderless
  windows, Alt+Tab, multiple monitors, mixed scaling, and mouse and keyboard
  handling. This includes launchers such as Battle.net, Ubisoft Connect,
  Rockstar Games Launcher, and the Windows Steam client.

- **Rendering and desktop integration.** Support for overlays, popups, login
  windows, and content rendered by another Windows process, along with tray
  icons, application menus, HDR, and colour management where the desktop and
  driver support them.

- **Graphics and texture streaming.** Maintained DXVK and VKD3D-Proton builds,
  plus an optional bundled RADV driver. RADV combines compatible sparse image
  bindings to reduce mapping operations. Matching kernel support enables
  further batching and avoids unnecessary waits on unrelated rendering.

- **Runtime overhead and synchronization.** Fixes for repeated Steam API
  initialization checks and controller discovery work, plus NTSync support
  and optional kernel acceleration for Windows I/O completion ports.

- **Video and media playback.** Compatibility work in Windows Media Foundation
  and the supporting media components, including launcher and in-game video.

- **Performance diagnostics.** A shared HUD for DXVK and VKD3D-Proton with
  FPS, lows, hardware telemetry, renderer and driver information, presentation
  mode, and presentation latency readings and graphs where supported.

Audio, input, synchronization, and rendering improvements are useful
independently of native Wayland. Some features are automatic, some are opt-in,
and some require capabilities from the driver, compositor, or kernel. The
sections below explain those differences.


Is it experimental?
-------------------

Yes, but "experimental" does not mean inherently unstable. Proton Wineland
takes a different approach from the usual pattern of relying on environment
variables, command line parameters, and Wine workarounds for individual
titles.

Where possible, it aims to solve the underlying problem in a durable way. This
does not mean the project has no bugs, including in newly added areas, but
being experimental does not mean deliberately sacrificing stability. In some
situations, it may even be more stable than other Proton versions.


How is artificial intelligence used?
------------------------------------

AI tools are a regular part of Proton Wineland's development process. I use
them to analyse logs, investigate problems, review changes, and help fix or
generate code when I consider that appropriate.

Before delivery, every commit goes through a review assisted by AI. I examine
the findings and decide whether the suggested changes are technically sound
and suitable for the project. When they are, I may let the AI modify the code
and then review the result again.

AI does not replace engineering judgement, testing, or responsibility for the
code that is delivered. Used thoughtfully, however, it is a valuable software
development tool that can accelerate investigation and help identify problems
that might otherwise be missed.


How do I activate the Wayland features?
----------------------------------------

Native Wayland is selected by default when a Wayland session is available,
through `PROTON_ENABLE_WAYLAND=1`. Games and supporting applications use it
automatically in that session.


How does audio hotplug work?
---------------------------

Native PipeWire audio is enabled by default. Wine tracks playback and capture
devices, their identities, and changes to the desktop's default devices while
the application is running. It exposes those changes through Windows audio
device notifications and can rediscover endpoints after a PipeWire restart.

USB DualSense and DualSense Edge support includes associating the controller's
HID and audio interfaces so games can find the audio endpoint used for
haptics. The game must support that feature. This does not add audio haptics
over Bluetooth.

To use the PulseAudio/ALSA path instead, set:

```sh
PROTON_USE_PIPEWIRE=0 %command%
```

Remove the option to restore the default. An explicit `WINE_AUDIO_DRIVER`
override takes precedence over the fallback selection.


How do I use the bundled RADV driver?
------------------------------------

For AMD Vulkan games, set this launch option:

```sh
PROTON_RADV=1 %command%
```

This selects the private `radv-salkim` build for that launch without replacing
your system driver or OpenGL libraries. GPUs from other vendors continue to use
their existing Vulkan drivers. Standard x86-64 builds include both 64-bit and
32-bit RADV libraries. The bundled driver is not available on ARM64.

The driver is built from the project's Mesa branch, based on upstream
development with additional fixes and optimizations. It can differ from your
distribution's stable Mesa version. Remove `PROTON_RADV=1` to use your system
Vulkan driver again.

Sparse image binding coalescing works with ordinary supported kernels. The
additional batch mapping, reduced synchronization, UserQ paths require matching
amdgpu capabilities. RADV checks the interface and advertised features at
startup rather than relying on a kernel name or version. Without that support,
it uses the standard per-operation mapping path. No custom kernel is required
to use the bundled RADV driver.


Do I need the Salkim kernel?
---------------------------

No. Proton Wineland works with supported distribution kernels. The separately
maintained [Salkim kernel](https://github.com/nanomatters/kernel-salkim)
provides additional interfaces for some optimizations:

- **amdgpu sparse mapping:** batch several mappings into one ioctl and use
  completed sparse dependencies to avoid unnecessary rendering waits.
- **NTSync I/O completion ports:** accelerate completion-port operations used
  by Windows applications for asynchronous work.

These are separate capabilities. RADV checks for its mapping interface, and
Wine probes for the NTSync IOCP extension. Ordinary NTSync support does not
imply IOCP acceleration. When that extension is absent, completion ports
remain managed by wineserver. If NTSync itself is unavailable, Wine uses its
other synchronization paths.

Using the bundled RADV driver does not install a kernel or turn on AMD user
queues. Kernel-specific optimizations are selected only when their required
interfaces are available.


How is the initial monitor selected?
------------------------------------

At launch, Proton reads KDE's or GNOME's configured primary monitor, niri's
focused monitor, or COSMIC's preferred display for X11 games. COSMIC's
preference is reused for native Wayland games. This does not switch them to
Xwayland. The query runs once, does not change desktop settings, and leaves
Wine's default unchanged if the desktop is unsupported or no unambiguous
answer is available.

An explicit `WAYLANDDRV_PRIMARY_MONITOR` always takes precedence, for example:

```sh
WAYLANDDRV_PRIMARY_MONITOR=DP-2 %command%
```


How do I enable the HUD with PROTON_HUD?
---------------------------------------

Set a preset in Steam's launch options:

```sh
PROTON_HUD=3 %command%
```

| Level | Information |
| --- | --- |
| `1` | FPS, 1% and 0.1% lows, and window system. |
| `2` | Adds GPU and CPU load, power, temperature, Proton and GPU driver versions, and the graphics API. |
| `3` | Adds GPU and CPU clocks, VRAM usage, renderer version, and presentation mode. |
| `4` | Adds presentation latency timings to level 3. |
| `5` | All HUD elements except `systeminfo`, arranged on the left. |

Levels 1 to 4 use centred horizontal rows, with window system information at
the bottom. HDR, direct scanout (DSO), and VRR status appear when available.

Press **Ctrl+Shift+O** to hide or show the HUD. Visibility updates once per
second, including while hidden. The shortcut uses `DXVK_HUD`, whether set
directly or generated by `PROTON_HUD`. An unset, empty or `0` value disables
the shortcut. An empty HUD does not poll for visibility.
Append `hide` to start with the HUD hidden, for example
`PROTON_HUD=3,hide %command%`. Then press Ctrl+Shift+O to show it.

This extends the existing DXVK HUD, which VKD3D-Proton also uses for Direct3D
12 games. Wine samples CPU and GPU telemetry once per second in background
threads when requested. Available readings depend on hardware, drivers, and
permissions.

The same HUD is available with `PROTON_DXVK_LOWLATENCY=1`. Its existing
`renderlatency`, `jitter` and `latencydetails` items remain available
alongside the Wineland HUD options.

Append DXVK HUD options after the preset to customise it, for example:

```sh
PROTON_HUD=3,scale=1.25,-gpu.temp %command%
```

Levels 3 and 4 already show the Vulkan presentation mode, such as `FIFO`,
`MAILBOX` or `IMMEDIATE`, in Direct3D 8 through 12 games. To add it to a lower
preset:

```sh
PROTON_HUD=1,present.mode %command%
```

This shows the renderer's selected mode. The compositor may still control how
frames reach the display. No latency telemetry is needed for this field.

Add history graphs to a preset when investigating frame pacing, for example:

```sh
PROTON_HUD=4,fps.graph,latency.queue.graph,latency.display.graph %command%
```

Presentation graphs need the same timing support as the readings below.

An explicit `DXVK_HUD` takes precedence. See the
[DXVK HUD options](https://github.com/nanomatters/dxvk-salkim#hud)
for individual metrics, layout, and appearance settings.

### What do the presentation latency readings measure?

`PROTON_HUD=4` adds these readings for DXVK and VKD3D-Proton when the driver
and presentation stack support `VK_EXT_present_timing`. All four are displayed
in milliseconds. They use three timestamps:

- `T0` is recorded by us shortly before calling `vkQueuePresentKHR`. In
  VKD3D-Proton this happens on the command queue's submission thread. Time queued
  between the application's DXGI `Present()` call and that point is excluded.
- `T1` is the Vulkan driver's `QUEUE_OPERATIONS_END` timestamp.
- `T2` is the Vulkan driver's `FIRST_PIXEL_OUT` timestamp when available.

The driver supplies the event timestamps through Vulkan presentation-timing
reports. We do not take new timestamps when the reports arrive. A report
arriving several frames later does not add to the measured latency. First
pixel out means data leaving the presentation engine toward the display, not
pixels already visible.

The original timing source depends on the driver and presentation stack.
Mesa's Wayland implementation gets first pixel out from the compositor's
presentation feedback. On KWin's DRM backend this normally comes from the
kernel page-flip completion timestamp for the frame containing the game's
image, not a measurement of the display's pixel response. Other drivers can
obtain timing differently.

| HUD label | Calculation | Meaning |
| --- | --- | --- |
| Queue latency | `T1 - T0` | Submission work, dependency waits, and queued presentation work. |
| Output latency | `T2 - T1` | Downstream presentation processing and waiting, which can include compositor work. |
| Present latency | `T2 - T0` | Total of the two stages for the same frame. |
| Output interval | `T2(current frame) - T2(previous frame)` | Spacing between consecutive frames reaching output. |

Output interval can be lower than Present latency because several frames can
be in flight. A frame can take 20 ms to reach output while frames emerge every
8 ms. These readings do not measure total CPU or GPU rendering time or full
input latency.

If first pixel out is unavailable, the latency endpoint falls back to first
pixel visible, then request dequeued. The last fallback does not confirm
display output. Output interval stays unavailable without consecutive first
pixel out measurements. Text refreshes every 500 ms using the latest valid
readings, not an average. Individual fields may come from different frames.

Wine disables presentation timing for surfaces eligible for its managed dmabuf
path, including cross-process presentation. Those surfaces can show `--` even
when the host driver supports the extension. This is a Wine presentation-path
limitation rather than missing driver support.

The configuration keywords remain `latency.queue`, `latency.display`,
`latency.present`, and `latency.interval`. See the
[measurement details and timeline](dxvk/README.md#presentation-latency-measurements).


How do I use OptiScaler nightlies?
---------------------------------

Use `PROTON_USE_OPTISCALER=1` for stable releases. For the latest official
nightly, set this Steam launch option:

```sh
PROTON_USE_OPTISCALER=nightly %command%
```

To pin a specific nightly:

```sh
PROTON_USE_OPTISCALER=nightly-20260906 %command%
```

Nightlies are downloaded from
[OptiScaler's official releases](https://github.com/optiscaler/OptiScaler-nightly/releases)
and verified before installation. Updates are checked at launch, not during
gameplay. Previous INI files are saved as `.ini.old`, and
`PROTON_OPTISCALER_CONFIG` is reapplied. Switch back to `1` to use stable
releases again.

Nightlies also fetch the latest official NVIDIA Streamline runtime for the
frame-generation combinations that require it. Its DLLs and licence notices
are installed under `umu/OptiScaler/streamline`, with links to the existing
managed DLSS DLLs instead of duplicate copies. Private nightly libraries stay
under `OptiScaler`. The entry DLL and INI stay in `umu`.

Nightlies use their bundled XeSS and FidelityFX libraries instead of fetching
extra standalone copies. Explicit upscaler upgrades remain available. Existing
installer-owned duplicates are removed only when unchanged. Modified files
and custom library paths are preserved.

The first Streamline download is the full SDK archive (currently about 276
MB). The installed runtime is about 10 MB. Downloads are verified and cached,
and a failed update keeps the previous installation without disabling
OptiScaler. Stable OptiScaler installations do not fetch Streamline
automatically.


What is the relationship to CachyOS Proton?
-------------------------------------------

Proton Wineland grew from CachyOS Proton and retains its build, integration,
and runtime foundations. Development now spans independently maintained Wine,
DXVK, VKD3D-Proton, and Mesa branches, as well as changes to the Proton
runtime and build itself.

Early on, Proton Wineland was regularly rebased onto the latest CachyOS
branches. As its Wayland work expanded, rebasing the Wine component became
increasingly difficult. Repeatedly resolving the growing number of conflicts
began to destabilise the codebase, so Proton Wineland now maintains its Wine
work independently and selectively picks compatible upstream and bleeding edge
changes.

An early possibility was contributing the Wineland changes directly to
CachyOS. As the project grew in scope and followed its own technical
direction, independent development became the better fit. This gives Proton
Wineland room to pursue broader solutions when they can improve the Proton
experience.

CachyOS has its own development priorities and established relationships with
the Wine development process. Proton Wineland complements that work with an
independent focus. I have great respect for the CachyOS maintainers and
developers and value their tremendous work.


Original Proton documentation
-----------------------------

The original Proton documentation, including build instructions, is available
in [README_PROTON.md](README_PROTON.md).
