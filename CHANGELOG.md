# Changelog

All notable changes to this project are documented here. This file was started retroactively after a long period of undocumented development — entries before this point are reconstructed from development history and aligned to the actual git tags below.

Versions follow [Semantic Versioning](https://semver.org/) loosely: **MAJOR** for breaking changes to the theme file format or protocol, **MINOR** for new features, **PATCH** for fixes.

## [Unreleased]

## [1.5.0] — Cross-Vendor GPU Support

### Added
- Standard GPU sensor keys (`GPU_USAGE`, `GPU_TEMP`, `GPU_FAN1`, `GPU_POWER`, `VRAM_USED`) now automatically work on both NVIDIA and AMD cards with no theme builder changes needed after a GPU swap. Each key checks vendor-specific candidate sensor names and resolves fresh on every read.
- `GPU_POWER` now uses AMD's `Total Board Power (TBP)` sensor (the real measured board-level total) rather than a single internal power rail, since AMD splits power across separate Core/GFX, SoC, and Memory rails with no combined sensor of its own.
- `VRAM_USED` deliberately avoids AMD's `GPU Memory Usage` sensor, which has a confirmed, longstanding driver bug acknowledged by HWiNFO's own author (can report values hundreds of GB too high). Uses `GPU D3D Memory Dedicated` instead, which is accurate and matches AMD's own software and Windows Task Manager.
- `VRAM_USAGE` (percentage) is now computed automatically from `VRAM_USED` and a built-in lookup table (`GPU_VRAM_GB`) of known GPU model names to VRAM capacity, since neither vendor exposes total VRAM capacity as a polled sensor. Checked once at startup (or on manual sensor re-discovery), not polled continuously, since VRAM capacity is static. Cards not in the table simply leave `VRAM_USAGE` unavailable rather than guessing; `VRAM_USED` keeps working regardless.
- New wiki page: [GPU Vendor Support](https://github.com/mike-novotny/sensor-panel/wiki/GPU-Vendor-Support), documenting all of the above in detail.

### Changed
- `build.bat` now automatically closes any running `DS916Tray.exe` before overwriting it, skips shortcut recreation if shortcuts already exist, and offers to relaunch the app immediately after a successful build — removing the need to manually uninstall, close, or relaunch between iterations during normal development.
- Clarified in README and wiki that **Uninstall** is for removing the app from a machine entirely, not a routine step before rebuilding — `build.bat` already handles closing/overwriting a running instance on its own.
- README's sensor key reference table updated to show vendor-specific source names where they differ, rather than implying one fixed sensor name per key.

## [1.4.0] — Logging System & HWiNFO Auto-Restart Redesign

### Added
- **Logging system.** All console output migrated from bare `print()` calls to Python's `logging` module, written to `%APPDATA%\DS916Tray\ds916_tray_log.txt`. Three configurable levels in **Settings → General → Logging**: `Off`, `Normal` (default — startup, theme loads, connection status, settings changes, errors), and `Verbose` (adds per-frame/per-sensor-read diagnostics for active troubleshooting). Log file is capped at ~1MB via `RotatingFileHandler` with one backup kept, so it can never grow unbounded. Added **📁 Open Log Folder** button for quick access.
- Settings changes are now explicitly logged (`Setting changed: key = old -> new`), including a guaranteed final log line when logging itself is switched off, so there's always a record of why logging stopped.

### Changed
- **HWiNFO auto-restart redesigned from the ground up.** The previous Windows Scheduled Task approach (added in 1.3.0, debugged extensively in 1.3.0–1.3.1) is removed entirely, replaced with an in-app periodic check: the tray app checks HWiNFO64's real process uptime every 30 minutes and restarts it once it's been running 11.5 hours. This requires **no Scheduled Task, no XML generation, and no UAC/elevation prompt at all** — restarting an ordinary application you already have permission to interact with is not an elevated action, unlike registering a Scheduled Task, which is why the old approach needed elevation in the first place. This also makes the restart timing immune to the power-cycle drift problem the old fixed-schedule approach had: there's no schedule to drift from, since the check re-evaluates real uptime live every time it runs.
- **Auto-restart is off by default**, and surfaced as an explicit opt-in checkbox in Settings → HWiNFO — restarting someone's HWiNFO64 without ever asking is presumptuous, and HWiNFO Pro license holders have no 12-hour limit at all, so defaulting to "on" would restart their app needlessly.
- Settings → HWiNFO tab simplified accordingly: no more Install/Remove Task buttons; replaced with the auto-restart checkbox and a live status line showing HWiNFO64's current uptime and time until the next restart (if enabled).
- Status window's HWiNFO section updated to reflect the new mechanism instead of querying the now-removed scheduled task.

### Removed
- All Scheduled-Task-related code: XML generation, `ShellExecuteW`/`runas` UAC handling, `schtasks` queries for install/remove/status.

### Documentation
- Added a Logging section (README and wiki's Tray App Settings page) documenting the new log levels, file location, rotation behavior, and the Open Log Folder button.
- Rewrote all HWiNFO auto-restart documentation (README, HWiNFO Setup wiki page, Tray App Settings wiki page, Troubleshooting wiki page) for the new opt-in, no-elevation mechanism, including cleanup instructions for anyone with a leftover `DS916_HWiNFO_Restart` scheduled task from a previous version.
- Added guidance on HWiNFO64's Polling Period setting (README and wiki's HWiNFO Setup page) — explains that sensor "jumpiness" on bar/ring elements is almost always caused by HWiNFO's own update rate (2000ms default), not anything in this project, and documents the commonly-used 500-1000ms range for a snappier feel along with real caveats (slow individual sensors can drag the effective rate below the configured value; per-sensor polling control only exists for S.M.A.R.T. and Embedded Controller sensors).

## [1.3.1] — Sensor Reading Reliability Fix

### Fixed
- **Stale sensor index bug.** `config.json` cached numeric HWiNFO shared-memory indices for standard sensor keys (`CPU_USAGE`, `GPU_TEMP`, etc.), persisted indefinitely across sessions. A per-theme-load auto-correction pass tried to refresh these by name, but any key that failed to match a hardcoded name candidate silently kept its old, potentially wrong index forever — with no visibility into the failure. This caused a real-world case of every sensor reading as 0 after HWiNFO's sensor count/ordering shifted between sessions; the only previous workaround was manually deleting `config.json`.
- `read_sharedmem()` now resolves every standard sensor key fresh **by name**, on every single read, with nothing cached across sessions — the same pattern the RTSS reader already used successfully for matching process names. Nothing can go stale because nothing is persisted.
- Removed a chunk of dead, unreachable orphaned code (a leftover fragment of the old `read_registry` function from the 1.2.0 registry-mode removal, with no `def` line of its own).

### Changed
- `config.json` no longer stores a `sensor_map` at all; `load_cfg()` actively strips any leftover `sensor_map` from an older config file to prevent this class of bug from resurfacing.
- `CUSTOM_N` (manually-wired, non-standard) sensors now resolve their index from the currently loaded theme's own embedded `sensorMap`, rather than a separate persisted global config.
- Renamed `ds916sensors.json` to `hwinfo_sensors.json` — it's general HWiNFO system sensor data, not specific to the DS916 device itself. Updated throughout code, README, and wiki.

### Added
- `diagnose_hwinfo.py` — a standalone diagnostic script for inspecting HWiNFO shared memory directly (live sensor scan by name, plus a saved-sensor-map staleness check), independent of the main tray app.

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
