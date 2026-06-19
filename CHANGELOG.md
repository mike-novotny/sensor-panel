# Changelog

All notable changes to this project are documented here. This file was started retroactively after a long period of undocumented development — entries before this point are reconstructed from development history and aligned to the actual git tags below.

Versions follow [Semantic Versioning](https://semver.org/) loosely: **MAJOR** for breaking changes to the theme file format or protocol, **MINOR** for new features, **PATCH** for fixes.

## [Unreleased]

## [1.3.0] — Installation & Organization Overhaul
`a7d0c2b` (HEAD)

### Changed
- `build.bat` now installs `DS916Tray.exe` and `theme_builder.html` into `%APPDATA%\DS916Tray\` after building, and creates Desktop + Start Menu shortcuts ("DS916 Tray") pointing at the installed copy. Keeps the exe, theme builder, config, discovered sensors, and themes all together for easy cleanup, instead of scattered wherever the project was extracted/built.
- Tray menu's **🎨 Open Theme Builder** now opens `theme_builder.html` from the AppData install location rather than looking next to wherever the exe happens to be running from.
- Uninstall now also removes the Desktop/Start Menu shortcuts and deletes `theme_builder.html`, config, and sensor data from AppData. Theme files and the exe itself are left in place (Windows won't let a running exe delete itself).
- Sensor palette in the theme builder reorganized: Motherboard is now its own category (previously mixed with fans); new dedicated Framerate category.
- `RTSS_FPS` ("Framerate — Live (RTSS)") added to the default visible sensor palette, no longer requiring the **＋ Sensors** picker.
- Preset elements now drop a single static label with the sensor's name, rather than a compound label+value+bar — matches original intent of a quick-start label.
- Theme builder palette icons: value/text elements now show **V**, label elements (static text, preset) now show **T**.

### Added
- `RTSS_FPS_MIN` / `RTSS_FPS_MAX` / `RTSS_FPS_AVG` sensors (session-based RTSS benchmark stats, opt-in via **＋ Sensors**).
- Sensor discovery now reads HWiNFO's device group names (e.g. "AMD Ryzen 5 5600X", "ASRock B550M Steel Legend") and surfaces them as static-label elements in a new **Device Names** section of the **＋ Sensors** picker.
- Discovered custom sensors now get smarter category assignment based on their parent device's name, rather than defaulting to a generic bucket.

### Fixed
- HWiNFO restart scheduled task XML used an invalid `RepetitionTrigger` element, which Task Scheduler rejected outright ("ERROR: The task XML contains an unexpected node"). Replaced with the correct `TimeTrigger` + `StartBoundary` + `Repetition` structure.
- Task creation/removal failed with "Access is denied" under normal (non-elevated) execution — `schtasks` requires elevation to register tasks. Both now use `ShellExecuteW` with the `runas` verb to trigger a proper UAC prompt.
- Restart task's first-run time was calculated as 11.5h from the moment **Install Restart Task** was clicked, not from when HWiNFO64 itself actually started. If HWiNFO64 had already been running for a while, this left a real window where shared memory could die before the task ever got a chance to fix it. The task now detects HWiNFO64's actual process start time via `GetProcessTimes` and aligns the first run to `start_time + 11.5h` instead.
- Removed `GPU Core Load` and `CPU (Tctl/Tdie)` from sensor auto-import — both were duplicates of `GPU_USAGE` and `CPU_TEMP` respectively from the same underlying HWiNFO entries.
- `MB_TEMP` label corrected from `"Motherboard"` to `"Motherboard Temp"`.
- Removed a duplicate `RAM_FREE_GB` key in the sensor definitions object.
- `Battery` removed from default visible sensors (still available via **＋ Sensors**).
- Removed the **Sensor Map** settings tab — it exposed raw numeric shared-memory indices with no practical guidance on when or how to use them; sensor mapping is handled correctly and invisibly by auto-discovery.

### Removed
- The "registry" HWiNFO data source fallback mode was already removed in 1.2.0; this release also removed the now-unnecessary **HWiNFO Source** Settings dropdown that used to switch between it and shared memory.

## [1.2.0] — RTSS Integration
`ef4c62e`

### Added
- Optional FPS sensor via RivaTuner Statistics Server (RTSS) shared memory — hooks directly into the game's render API for accurate per-process framerate, avoiding the background-app confusion of HWiNFO's free-tier PresentMon integration.
- Auto-detect mode (follows whichever hooked 3D app most recently rendered a frame) with manual process-pinning override in **Settings → RTSS (FPS)**, including a live "Refresh List" of currently active 3D applications.
- Graceful degradation when RTSS isn't installed or running — sensor simply stays unavailable, no errors or crashes.

### Changed
- AI Theme Generator backgrounds now rasterize to PNG before saving, rather than embedding raw SVG.

### Fixed
*(extensive debugging during initial RTSS implementation, all resolved within this release)*
- `MapViewOfFile` access-denied errors traced to requesting a fixed 8MB mapping size larger than RTSS's actual shared memory section — fixed by requesting size `0` to map the real existing section instead of guessing.
- RTSS's signature bytes did not match the assumed `'RTSS'` byte order — corrected via live testing to the actual in-memory byte sequence.
- Framerate value initially read from `dwStatFramerateAvg`, which is tied to RTSS's benchmark/recording session lifecycle and would spike then drop to zero outside of an active session. Switched to `dwStatFrameTimeBufFramerate`, a continuously-updating ring buffer value with no session dependency.
- Reconnection attempts were retried every single frame with no backoff, spamming the console; added a 10-second retry cooldown.
- HWiNFO shared memory handle going stale after HWiNFO64 restarts (e.g. after the 12-hour free-version limit) was not being detected, silently blocking reconnection until the whole tray app was restarted. Both HWiNFO and RTSS readers now re-verify their signature on every read and reset their handle if it's gone stale.
- AI-generated backgrounds were embedded as raw SVG, which the tray app's image library (Pillow) cannot decode — silently resulting in a blank background on-device despite looking correct in the browser preview.

### Removed
- The "registry" HWiNFO data source fallback mode. It called an undefined function (`read_registry`) and had likely never worked — surfaced only once HWiNFO64's shared memory genuinely became unavailable during a 12-hour-limit test. Shared memory is now the only data source; it degrades gracefully (sensors simply go unavailable) if HWiNFO64 isn't running.

## [1.1.0] — Theme Builder Feature Expansion
`499f68e`

### Added
- Segmented bar and ring styles (`solid` / `segmented` / `gapped`), with configurable segment count, gap, and cap style — LED-meter and tick-mark aesthetics alongside the original solid fill.
- Multi-series line graphs — up to 3 sensors per graph, with independent left/right Y-axes for sensors on very different scales (e.g. temperature alongside usage percentage).
- AI Theme Generator: 12 visual "vibes" (Space, Cyberpunk, Minimal, Nature, Racing, Anime, Synthwave, Industrial, Ocean, Monochrome, Volcanic, Aurora), each with multiple randomized structural layout templates and per-generation color jitter, fully offline/rule-based (no external API, no cost).
- GitHub Wiki documentation set: installation, protocol reverse-engineering, theme builder usage, sensor reference, HWiNFO/RTSS setup, troubleshooting, and more.

### Fixed
- Double confirmation dialogs when generating an AI theme or loading a theme over existing canvas content (two separate "clear canvas" code paths were each triggering their own popup).
- AI-generated layouts originally all looked structurally identical per vibe; added genuine layout variety via multiple randomly-selected templates per orientation.
- Horizontal-orientation layouts could overflow the canvas height with unbounded text columns; added column-wrapping logic based on available height.
- A dropped `setOri('vertical')` initialization call left the canvas at zero size, silently breaking all element placement.

### Changed
- `FRAMERATE` (HWiNFO/PresentMon) removed from default sensors and documented as unreliable without an HWiNFO Pro license.
- Added README guidance recommending an HWiNFO Pro license to support continued development of the sensor monitoring engine this project depends on.

## [1.0.0] — Initial Working System
`0b2d3b4`

### Added
- Reverse-engineered USB protocol for the Jonsbo DS916 LCD panel (ArtInChip-based, JPEG-over-serial with a 60-byte frame header) — full protocol documented from scratch via Wireshark/USBPcap analysis, no official documentation existed.
- Python tray application (`ds916_tray.py`): renders themes to the display, reads sensor data from HWiNFO64 shared memory, system tray integration, Windows startup registration, theme loading.
- Browser-based visual theme builder (`theme_builder.html`): drag-and-drop canvas editor, sensor palette, text/bar/ring/graph elements, custom fonts, background images, theme save/load as self-contained JSON.
- HWiNFO64 shared memory sensor reading, with automatic sensor discovery saved to a local JSON file for the theme builder to consume.
- Workaround for HWiNFO64 free edition's 12-hour shared memory limit via a Windows Scheduled Task that periodically restarts HWiNFO64.
