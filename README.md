# DS916 Sensor Panel

A fully open-source sensor panel system for the **Jonsbo DS916** (and compatible ArtInChip-based USB LCD screens), built by reverse-engineering the device's USB protocol from scratch.

Design custom themes visually, stream live hardware sensor data from HWiNFO64, and run everything silently in the background — no proprietary software required. Don't want to design from scratch? The built-in **✨ AI Theme Generator** can build a complete, good-looking theme for you in one click.

> This project relies entirely on [HWiNFO64](https://www.hwinfo.com) for sensor data. If you find this useful, please consider a [HWiNFO Pro license](https://www.hwinfo.com/buy/) — it removes the shared memory time limit and supports the developer of the tool this whole project is built on.

---

## Background — How This Was Discovered

The Jonsbo DS916 ships with the **JONSBO-AIO** application, which is the only officially supported way to use the screen. There was no public documentation of how the device communicates with a PC.

Through USB traffic analysis using Wireshark and USBPcap, we reverse-engineered the complete communication protocol:

### Device Identification

The DS916 enumerates as two USB devices:

| Device | VID | PID | Description |
|--------|-----|-----|-------------|
| Composite | `0x33C3` | `0xF101` | ArtInChip USB composite device |
| Serial | `0x33C3` | `0xF101 MI_00` | Virtual COM port (CDC ACM) |

The chip inside is made by **ArtInChip** (广东匠芯创科技有限公司), a Chinese semiconductor company. Their SDK (`Luban-Lite`) is open source and uses **CherryUSB** as the USB stack. The screen presents itself as a USB CDC serial device (`COM3` on most systems).

The manufacturer DLL bundled with JONSBO-AIO — `MSDISPLAYSDKWRRAPER.dll` — contains an embedded copy of **libjpeg-turbo**, which confirmed our hypothesis that the screen receives JPEG frames rather than raw pixel data.

### Protocol

The DS916 accepts a continuous stream of **JPEG frames** over the CDC serial COM port. Each frame consists of a **60-byte header** followed by the raw JPEG data.

**Frame structure:**
```
[60-byte header][JPEG data]
```

**Header format (60 bytes, little-endian):**
```
Offset  Size  Value         Description
------  ----  -----         -----------
0       1     0x03/0x00     Frame type: 0x03 on first frame, 0x00 thereafter
1       1     0x3C (60)     Header length
2-4     3     0x000000      Reserved
5       1     0x06          Channel/endpoint identifier
6-7     2     0x0000        Reserved
8-11    4     uint32 LE     Total packet size (JPEG size + 60)
12-16   5     0x00...       Reserved
17      1     0x4F ('O')    \
18      1     0x54 ('T')     > Magic bytes: "OT\x06"
19      1     0x06          /
20-23   4     (varies)      Timestamp/counter — device ignores this, zero is fine
24      1     0x1B          Constant
25-31   7     (varies)      Sequence-related — device ignores, zero is fine
32      1     0x00          Reserved
33      1     0x1B          Constant
34      1     0x00          Reserved
35-38   4     (varies)      Frame group counter — device ignores, zero is fine
39-43   5     89 B3 FF FF   Constant
              00
44-55   12    00 00 00 09   Constant protocol flags
              00 00 01 00
              03 00 02 03
56-59   4     uint32 LE     JPEG payload size (confirmed — must be exact)
```

**Key findings:**
- The baud rate setting is irrelevant — the device operates at USB 2.0 speeds regardless
- Only bytes 0, 8-11, and 56-59 need to be set correctly; all timestamp/counter fields can be zeroed
- Frames should be sent continuously at 5-10 fps to keep the display active
- Screen resolution is **462 × 1920 pixels** (portrait) or **1920 × 462** (landscape)
- JPEG quality of 85-92 gives a good balance of quality and throughput

**Minimum working Python example:**
```python
import serial, struct, io
from PIL import Image

def make_frame(jpeg_bytes, first=False):
    header = bytearray(60)
    header[0]  = 0x03 if first else 0x00
    header[1]  = 0x3C
    header[5]  = 0x06
    header[17] = 0x4F  # 'O'
    header[18] = 0x54  # 'T'
    header[19] = 0x06
    header[24] = 0x1B
    header[33] = 0x1B
    header[39:44] = bytes([0x89, 0xB3, 0xFF, 0xFF, 0x00])
    header[44:56] = bytes([0x00,0x00,0x00,0x09,0x00,0x00,0x01,0x00,0x03,0x00,0x02,0x03])
    struct.pack_into('<I', header, 56, len(jpeg_bytes))
    struct.pack_into('<I', header,  8, len(jpeg_bytes) + 60)
    return bytes(header) + jpeg_bytes

img = Image.new('RGB', (462, 1920), color=(20, 20, 40))
buf = io.BytesIO()
img.save(buf, format='JPEG', quality=88)
jpeg = buf.getvalue()

port = serial.Serial('COM3', baudrate=115200, timeout=2)
for i in range(60):
    port.write(make_frame(jpeg, first=(i==0)))
    port.flush()
port.close()
```

---

## Project Structure

```
sensor-panel/
├── theme_builder.html    # Visual theme designer (open in Chrome or Edge)
├── ds916_tray.py         # Background renderer + system tray app
├── build.bat             # One-click build script (creates DS916Tray.exe)
└── README.md
```

---

## Step-by-Step Installation

### Step 1 — Install Python

Download and install Python 3.9 or later from https://www.python.org/downloads/

> **Important:** During installation, check **"Add Python to PATH"**

Verify it works by opening a Command Prompt and running:
```
python --version
```

### Step 2 — Install HWiNFO64

1. Download HWiNFO64 from https://www.hwinfo.com/download/
2. Install and launch it
3. Open **Settings** (wrench icon) and go to the **General/User Interface** tab
4. Enable the following settings:

| Setting | Why |
|---------|-----|
| ✅ **Minimize Main Window on Startup** | Keeps HWiNFO out of the way at startup |
| ✅ **Minimize Sensors on Startup** | Sensors run in the background |
| ✅ **Minimize Sensors instead of Closing** | Prevents accidentally stopping sensor data |
| ✅ **Auto Start** | HWiNFO launches automatically with Windows |
| ✅ **Shared Memory Support** | **Required** — allows the tray app to read sensor data |

> **Optional:** Uncheck **Automatic Update** to prevent HWiNFO from showing update popups. If you disable this you will need to manually check for updates at https://www.hwinfo.com

5. Click **OK** and close Settings
6. HWiNFO will now start minimized to the system tray on every boot

### Step 3 — Download the Sensor Panel Files

Download or clone this repository:
```
git clone https://github.com/mike-novotny/sensor-panel.git
```
Or download the ZIP from the GitHub page and extract it to a folder of your choice.

### Step 4 — Build the Tray App

Open a Command Prompt **in the folder where you extracted the files** and run:
```
build.bat
```

This will:
1. Install all Python dependencies (`pillow`, `pyserial`, `pystray`, `pyinstaller`)
2. Compile `ds916_tray.py` into a standalone `dist\DS916Tray.exe`
3. Copy `theme_builder.html` into the `dist\` folder

> The build takes 1-3 minutes. When it finishes everything you need is in the `dist\` folder.

### Step 5 — First Launch

1. Navigate to the `dist\` folder
2. Double-click **`DS916Tray.exe`**
3. A small icon appears in your system tray (bottom-right of the taskbar)
4. The app will:
   - Add itself to Windows startup automatically
   - Connect to HWiNFO64 shared memory
   - **Discover all your sensors** and save them to `%APPDATA%\Roaming\DS916Tray\ds916sensors.json`
   - Create a `%APPDATA%\Roaming\DS916Tray\Themes\` folder for storing your theme files

> **If you've ever used JONSBO-AIO, make sure it's fully closed** (including from the system tray) before running DS916Tray — both cannot use the COM port at the same time. If you've never installed or used JONSBO-AIO, you can skip this.

### Step 6 — Design a Theme

1. Open **`theme_builder.html`** in Chrome or Edge
2. When prompted, click **Browse for file…** and navigate to `%APPDATA%\Roaming\DS916Tray\ds916sensors.json` to load your system's sensor list
3. Design your theme (see [Theme Builder](#theme-builder) section below)
4. Click **💾 Save Theme** and save to `%APPDATA%\Roaming\DS916Tray\Themes\`

### Step 7 — Load the Theme

1. Right-click the DS916 tray icon
2. Click **📂 Load Theme…**
3. Navigate to `%APPDATA%\Roaming\DS916Tray\Themes\` and select your `.ds916theme` file
4. The screen should immediately start displaying your theme

---

## Preparing Image Assets

If you plan to use a background image or image layers, they must match the screen's resolution exactly.

### Canvas Dimensions

| Orientation | Width | Height |
|-------------|-------|--------|
| **Vertical** (default) | 462 px | 1920 px |
| **Horizontal** | 1920 px | 462 px |

Images that don't match these dimensions will be stretched to fit. For best results, create your background at exactly 462×1920 px (or 1920×462 px for landscape).

### Recommended Tools

Any image editor works — Photoshop, GIMP, Affinity Photo, or even Paint.NET. Create a new canvas at the correct dimensions, design your background, and export as PNG or JPG.

For animated backgrounds, MP4 video files are supported (AVI and WMV also work). The video loops automatically and plays silently.

### Background vs Image Layers

The theme builder has two separate ways to add visual assets:

**🖼 Background button** — sets the canvas background. This can be:
- A static image (PNG, JPG, GIF)
- A video file (MP4, AVI, WMV) — plays looped and silent behind all elements

**+ Image button** — adds a transparent image layer **on top of** the background and behind or in front of sensor elements (controlled by z-order). Use this for:
- Decorative overlays (borders, frames, logos)
- Semi-transparent panels behind groups of sensors
- Static artwork that sits above a video background

You can add multiple image layers. Each behaves like any other element — drag to reposition, resize with the handle, adjust z-order in the Layers panel.

---

## Tray App — Right-Click Menu

| Option | Description |
|--------|-------------|
| **▶ Start Display** | Start streaming the current theme to the screen |
| **⏹ Stop Display** | Stop streaming |
| **📂 Load Theme…** | Load a `.ds916theme` file |
| **🎨 Open Theme Builder** | Opens `theme_builder.html` in your browser |
| **🔍 Discover Sensors** | Re-scan HWiNFO64 and update `ds916sensors.json` |
| **ℹ Status…** | Live status: sensor source, COM port, sensor readings |
| **⚙ Settings…** | COM port, FPS, HWiNFO source, sensor map, auto-restart |
| **🗑 Uninstall…** | Remove from startup and delete app data |
| **❌ Exit** | Close the tray app |

---

## Settings

Open via tray icon → **⚙ Settings…**

### General Tab

| Setting | Description |
|---------|-------------|
| **COM Port** | Serial port for the DS916 — click **Auto-detect** to find it automatically |
| **FPS** | Frames per second to stream (default: 6, max: 30) |
| **Theme File** | Path to the last loaded theme (auto-remembered) |
| **Start with Windows** | Adds/removes the app from Windows startup |

> **COM port auto-detection:** the app identifies the DS916 by its USB VID/PID (`33C3:F101`) and automatically updates the COM port setting if it changes between sessions.

### HWiNFO Tab

Configures the **11.5-hour auto-restart workaround** for HWiNFO64 free edition's shared memory time limit:

1. Click **Detect** to find `HWiNFO64.exe` automatically
2. Click **Install Restart Task** — Windows will show a **UAC prompt**; click Yes to allow it. The tray app itself never needs to run as Administrator, only this one-time task registration step does.
3. Status shows whether the task is currently installed

### RTSS (FPS) Tab

Configures the optional RivaTuner Statistics Server framerate source (see [Framerate Sensors](#framerate-sensors) above):

- **Connection status** — shows whether RTSS is currently running and how many active 3D applications it's tracking
- **Auto-detect active 3D app** (default) — automatically uses whichever hooked game most recently rendered a frame
- **Pin a specific process** — choose an exact process from a live dropdown (populated via **↻ Refresh List**) instead of relying on auto-detection, useful if you regularly run multiple games/3D apps at once

### Sensor Map Tab

Maps sensor keys (`CPU_USAGE`, `GPU_TEMP`, etc.) to their HWiNFO shared memory index. Populated automatically by sensor discovery — you should rarely need to edit this manually.

---

## Theme Builder

Open `theme_builder.html` in **Chrome or Edge** (not Firefox — system font loading requires Chrome/Edge).

### Interface Layout

| Area | Description |
|------|-------------|
| **Left panel** | Element palette — drag or click to add to canvas |
| **Center** | Canvas — drag elements to move, resize handle to resize |
| **Right panel** | Properties for the selected element |
| **Bottom** | Layers panel — visibility, z-order, delete |
| **Top bar** | Orientation, zoom, background, export controls |

### Adding a Background

1. Click **🖼 Background** in the top bar
2. Select a PNG, JPG, GIF, or video file (MP4, AVI, WMV)
3. The background appears behind all elements on the canvas
4. The background is embedded into the `.ds916theme` file on export — no separate file needed

To add decorative image layers on top of the background, use **+ Image** instead. These layers can be positioned, resized, and z-ordered like any other element.

### Loading Your Sensor List

Click **🔍 Sensors** in the top bar to import `ds916sensors.json`. This loads all sensors from your specific hardware into the palette. Without this file, only standard sensor keys are available.

The sensor list is generated automatically by the tray app on every startup. If the file doesn't exist yet, run DS916Tray.exe first.

### Element Types

| Type | Description |
|------|-------------|
| **Clock** | Live time — 12h (no seconds, no leading zero) or 24h |
| **Clock (seconds)** | Live time with seconds — 12h zero-padded for stable AM/PM position |
| **Date** | Live date — configurable format (DD-MM-YYYY, MM/DD/YYYY, etc.) |
| **Day of Week** | Full (Tuesday) or short (Tue) |
| **Sensor Value** | Live sensor reading with optional prefix and unit |
| **Static Label** | Fixed text — double-click to edit inline |
| **Bar** | Horizontal progress bar |
| **Ring Gauge** | Circular gauge |
| **Line Graph** | Scrolling history graph |
| **Preset** (⊞) | Drops a label + value + bar group in one click |
| **Rectangle** | Decorative divider or block |
| **Image** | PNG/JPG overlay layer (above background) |

### Sensor Palette

The palette shows a curated default set of sensors. Use the **＋ Sensors** button to open the full picker with checkboxes — select any sensor to add it to the palette. Importing a `ds916sensors.json` file adds your hardware-specific sensors (custom fans, liquid cooling temps, per-core data, etc.) to the picker.

### Resizing Elements

- **Text elements** — drag the resize handle (bottom-right corner) to scale **font size**
- **Bars, graphs, rings, rectangles** — drag the resize handle to change **width/height**
- **Arrow keys** — nudge 1px; Shift+arrow nudges 10px

### Fonts

In the Font section of the Properties panel:
- **🔍 System Fonts** — loads all fonts installed on your PC (Chrome/Edge only)
- **📁 Font File…** — loads a `.ttf` or `.otf` file; embedded in the theme on export so it works on any system

### Colors

Each color field has a color picker, an alpha (0-255) transparency field, a **+** button to save to the palette, and saved color swatches. Click any swatch to apply it.

### Export & Import

- **💾 Save Theme** — saves a single `.ds916theme` file with all assets (background, fonts, image layers) embedded as base64. The file picker defaults to `%APPDATA%\DS916Tray\Themes\` after first use
- **📂 Import** — loads a `.ds916theme` file. The file picker remembers the Themes folder

---

## ✨ AI Theme Generator

Don't want to design a theme by hand? Click **✨ AI Generate** in the top bar to instantly build a complete, good-looking theme — no design skills required.

This is a fully offline, rule-based generator (no internet connection, no API calls, no cost). It works by combining:

- **12 curated visual styles** ("vibes"), each with its own color palette, font, corner-radius personality, and a procedurally generated SVG background (gradients, starfields, scanlines, grid horizons, aurora bands, etc. — all drawn in code, no external image files)
- **7 distinct layout templates** (4 vertical, 3 horizontal), covering different arrangements: ring gauges up top, stacked progress bars, a featured line graph, or a clean single column
- **Randomization** — every time you generate, the layout template, sensor display types (ring vs. bar), color tone, and element ordering vary slightly, so generating twice with the same vibe won't produce an identical result

### How to Use It

1. Click **✨ AI Generate**
2. Choose **vertical** or **horizontal** orientation
3. Click a vibe thumbnail — Space, Cyberpunk, Minimal, Nature, Racing, Anime, Synthwave, Industrial, Ocean, Monochrome, Volcanic, or Aurora
4. Check or uncheck sensors in the list (a sensible default selection is pre-checked: clock, date, CPU usage/temp/fan, GPU usage/temp, motherboard temp, and chassis fans)
5. Click **Generate Theme**

The canvas is populated instantly with a complete layout — clock, date, labeled ring gauges or bars for usage-type sensors, and label+value+bar rows for everything else. Every ring/bar/graph element includes a label so it's always clear which sensor it represents.

From there, treat it like any other theme: drag elements to fine-tune positions, change colors, swap fonts, or just save it as-is with **💾 Save Theme**.

> Generating again will clear the current canvas (you'll be asked to confirm), so if you like a particular result, save it before trying another vibe or generating again.

---

## Sensor Discovery

When the tray app starts with HWiNFO64 shared memory enabled, it automatically scans all available sensors and saves them to:

```
%APPDATA%\Roaming\DS916Tray\ds916sensors.json
```

This file contains every sensor HWiNFO64 exposes, including hardware-specific sensors like additional fan headers, liquid cooling temperatures, per-core data, and framerate metrics.

**In the Theme Builder**, click **🔍 Sensors** to import this file. All discovered sensors appear in the element palette via the **＋ Sensors** picker.

**Re-run discovery** at any time via tray icon → **🔍 Discover Sensors** — for example after adding new hardware or updating HWiNFO64.

> Sensor indices vary between systems and HWiNFO versions. The tray app matches sensors by name (`"Total CPU Usage"`, `"CPU (Tctl/Tdie)"`, `"Framerate Displayed (avg)"` etc.) rather than by index, so mappings stay correct even if the order changes.

### Framerate Sensors

There are two ways to get FPS data, with very different reliability:

**`FRAMERATE` (via HWiNFO/PresentMon)** — HWiNFO64 can expose framerate via its PresentMon integration (sources like `Framerate Displayed (avg)`). In practice this is **unreliable without an HWiNFO Pro license** — the free version doesn't let you exclude background applications from PresentMon tracking, so it frequently reports the framerate of the wrong window instead of your game. For this reason `FRAMERATE` is **not included in the default sensor palette**, though it's still available via the **＋ Sensors** picker.

**`RTSS_FPS` (via RivaTuner Statistics Server)** — a much more reliable alternative, since RTSS hooks directly into the game's DirectX/OpenGL/Vulkan present calls rather than relying on system-wide PresentMon sampling. This means the framerate is correctly attributed to the actual game process, with no background-app confusion and no Pro license needed.

To use it:
1. Install [RivaTuner Statistics Server](https://www.guru3d.com/download/rtss-rivatuner-statistics-server-download/) (also bundled with MSI Afterburner) and make sure it's running
2. Launch your game — RTSS will automatically hook into it
3. Add the **`RTSS_FPS`** sensor via the **＋ Sensors** picker in the theme builder
4. In the tray app, go to **Settings → RTSS (FPS)** to confirm the connection status, and optionally pin a specific process by name instead of relying on auto-detection (which picks whichever hooked game most recently rendered a frame)

RTSS support is fully optional — if RTSS isn't installed or running, the `RTSS_FPS` sensor simply stays unavailable and everything else continues working normally. No administrator/elevated privileges are required.

**If `RTSS_FPS` shows 0 or no data for a specific game**, the most likely cause isn't a connection problem — it's that RTSS hasn't actually hooked that game yet. Check RTSS's own **Application Detection Level** setting (Options → General): if it's set to **Low**, try **Medium** or **High** instead, since some games need more aggressive hook injection to be detected. RTSS's on-screen display can be left **off** and **Stealth Mode** can be left **on** — neither affects whether shared memory data is available to this tray app.

---

## HWiNFO64 Shared Memory — 12-Hour Limit

HWiNFO64 free edition disables shared memory after 12 hours of continuous operation. When this happens, sensor values will stop updating until HWiNFO64 is restarted.

**Solutions:**

1. **Auto-restart task** (recommended) — Settings → HWiNFO tab → Install Restart Task. Creates a Windows Scheduled Task that silently restarts HWiNFO64 every 11.5 hours with no window appearing
2. **Manual restart** — restart HWiNFO64 manually when needed; the tray app will automatically reconnect to the fresh shared memory session within a moment

Check the current source at any time via tray icon → **ℹ Status…**

> **Please consider supporting HWiNFO64.** This entire project depends on HWiNFO's excellent and freely available sensor monitoring engine — without it, none of this would be possible. A [HWiNFO Pro license](https://www.hwinfo.com/buy/) removes the 12-hour shared memory limit entirely (making the restart workaround unnecessary), adds remote monitoring, and supports continued development of a tool the whole PC hardware community relies on. It's inexpensive and a fair trade for the years of free, high-quality work that's gone into it.

---

## Status Window

Right-click tray icon → **ℹ Status…** to see a live dashboard:

- **Display** — streaming status, COM port, FPS, active theme name and resolution
- **HWiNFO64 Sensor Source** — shows `Shared Memory ✅` or an unavailable warning if HWiNFO64 isn't running, plus whether the auto-restart task is installed
- **Live Sensor Snapshot** — current values for CPU/GPU usage and temperature, motherboard temp, CPU fan
- **System** — Windows startup status

The window auto-sizes to its content (capped at 90% of your screen height) and has a **↻ Refresh** button to re-read all values.

---

## How JONSBO-AIO Works (Internals)

Based on our reverse engineering:

1. Reads a **`Setting.txt`** layout file defining element positions, sensor bindings, fonts and colors
2. Reads hardware sensor values via Windows APIs at a configurable polling interval
3. Composites all elements onto a canvas using **SkiaSharp**
4. Encodes the result as JPEG using **libjpeg-turbo**
5. Sends the JPEG to COM3 continuously at ~6fps using the protocol documented above

The `MSDISPLAYSDKWRRAPER.dll` is the PC-side SDK wrapper from ArtInChip. The device firmware runs on their **Luban-Lite** RTOS with CherryUSB.

---

## Theme File Format

Themes are saved as a single **`.ds916theme`** file — a JSON document with all assets embedded as base64 data URLs.

```json
{
  "name": "MyTheme",
  "width": 462,
  "height": 1920,
  "background": "#111114",
  "backgroundImage": "data:image/png;base64,...",
  "sensorMap": { "CPU_USAGE": 46, "CPU_TEMP": 94 },
  "themeColors": ["#00b4ffff", "#ff0000ff"],
  "visibleSensors": ["CLOCK", "CPU_USAGE", "CPU_TEMP"],
  "customFonts": [{"family": "MyFont", "filename": "MyFont.ttf", "data": "data:font/ttf;base64,..."}],
  "elements": [ ... ]
}
```

### Element Types Reference

| Type | Key properties |
|------|----------------|
| `clock` | `clockFormat` (12h/24h), `clockSeconds` (true/false) |
| `date` | `dateFormat` (DD-MM-YYYY etc.) |
| `weekday` | `weekdayFormat` (full/short) |
| `text` | `sensorKey`, `prefix`, `unit`, `manualSize` |
| `static` | `customText`, `manualSize` |
| `bar` | `sensorKey`, `maxValue`, `fillColor`, `bgColor`, `cornerRadius` |
| `ring` | `sensorKey`, `maxValue`, `arcColor`, `trackColor`, `ringWidth` |
| `linegraph` | `sensorKey`, `maxValue`, `lineColor`, `historySeconds` |
| `rect` | `fillColor`, `cornerRadius` |
| `image` | `data` (base64 data URL) |

> **`manualSize`**: `false` (default for text) = box auto-sizes to content, resize handle scales font size. `true` = fixed dimensions, resize handle scales the widget.

### Standard Sensor Keys

| Key | HWiNFO Source Name |
|-----|-------------------|
| `CPU_USAGE` | Total CPU Usage |
| `CPU_TEMP` | CPU (Tctl/Tdie) |
| `CPU_FAN` | CPU1 |
| `GPU_USAGE` | GPU Core Load |
| `GPU_TEMP` | GPU Temperature |
| `GPU_FAN1` / `GPU_FAN2` | GPU Fan1 / GPU Fan2 |
| `VRAM_USAGE` | GPU Memory Usage |
| `MB_TEMP` | Motherboard |
| `CHASSIS_FAN1/2/3` | Chassis1 / Chassis2 / Chassis3 |
| `RAM_USAGE` | Physical Memory Load |
| `RAM_USED_GB` | Physical Memory Used |
| `RAM_TOTAL` | Physical Memory Total |
| `DISK_READ` / `DISK_WRITE` | Read Rate / Write Rate |
| `NET_DOWN` / `NET_UP` | Current DL rate / Current UP rate |
| `FRAMERATE` | Framerate Displayed (avg) — via HWiNFO/PresentMon, unreliable w/o Pro |
| `RTSS_FPS` | Live FPS — via RivaTuner Statistics Server (optional, see above) |
| `CUSTOM_N` | Any sensor at shared memory index N |

---

## Uninstalling

Right-click the tray icon → **🗑 Uninstall…**

This will:
1. Remove DS916Tray from Windows startup
2. Delete `%APPDATA%\Roaming\DS916Tray\` (config, sensor data, themes folder)
3. Close the app

Then manually delete `DS916Tray.exe` and `theme_builder.html`.

---

## Compatible Devices

Confirmed working:
- **Jonsbo DS916** ✅

If you get this working on another device, please open an issue or PR.

---

## Contributing

Pull requests welcome. Areas that would benefit most:

- **More compatible devices** — test on other ArtInChip-based panels
- **Theme gallery** — share your `.ds916theme` files in Discussions
- **Linux/Mac support** — protocol is the same; HWiNFO integration would need replacing (e.g. `lm-sensors`)
- **More sensor keys** — per-core temps, disk activity, GPU power, etc.

---

## License

MIT — do whatever you want with it.

---

## Acknowledgements

Protocol reverse-engineered using Wireshark + USBPcap on Windows 11.
HWiNFO shared memory format: https://gist.github.com/namazso/0c37be5a53863954c8c8279f66cfb1cc
ArtInChip Luban-Lite SDK: https://github.com/artinchip/luban-lite
CherryUSB: https://github.com/cherry-embedded/CherryUSB
HWiNFO64: https://www.hwinfo.com
