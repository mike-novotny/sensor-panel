# DS916 Sensor Panel

A fully open-source sensor panel system for the **Jonsbo DS916** (and compatible ArtInChip-based USB LCD screens), built by reverse-engineering the device's USB protocol from scratch.

Design custom themes visually, stream live hardware sensor data from HWiNFO64, and run everything silently in the background — no proprietary software required.

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
| ✅ **Minimize Sensors on Startup** | Same — sensors run in the background |
| ✅ **Minimize Sensors instead of Closing** | Prevents accidentally stopping sensor data |
| ✅ **Auto Start** | HWiNFO launches automatically with Windows |
| ✅ **Shared Memory Support** | **Required** — allows the tray app to read sensor data |

> **Optional:** Uncheck **Automatic Update** to prevent HWiNFO from showing update popups. If you disable this you will need to manually check for updates at https://www.hwinfo.com

5. Click **OK** and close the Settings window
6. HWiNFO will now start minimized to the system tray on every boot

### Step 3 — Download the Sensor Panel Files

Download or clone this repository:
```
git clone https://github.com/mike-novotny/sensor-panel.git
```
Or download the ZIP from the GitHub page and extract it to a folder of your choice (e.g. `D:\SensorPanel\`).

### Step 4 — Build the Tray App

Open a Command Prompt **in the folder where you extracted the files** and run:
```
build.bat
```

This will:
1. Install all Python dependencies (`pillow`, `pyserial`, `pystray`, `pyinstaller`)
2. Compile `ds916_tray.py` into a standalone `dist\DS916Tray.exe`
3. Copy `theme_builder.html` into the `dist\` folder

> The build takes 1-3 minutes. When it finishes you'll find everything you need in the `dist\` folder.

### Step 5 — First Launch

1. Navigate to the `dist\` folder
2. Double-click **`DS916Tray.exe`**
3. A small icon will appear in your system tray (bottom-right of the taskbar)
4. The app will:
   - Add itself to Windows startup automatically
   - Connect to HWiNFO64 shared memory
   - **Discover all your sensors** and save them to `%APPDATA%\Roaming\DS916Tray\ds916sensors.json`

> **Make sure JONSBO-AIO is fully closed** (including from the system tray) before running DS916Tray — both cannot use COM3 at the same time.

### Step 6 — Design a Theme

1. Open **`theme_builder.html`** in Chrome or Edge (not Firefox — system font loading requires Chrome/Edge)
2. When prompted, click **Import Sensor File** and navigate to:
   `%APPDATA%\Roaming\DS916Tray\ds916sensors.json`
   This loads all your specific hardware sensors into the palette
3. Drag elements from the left panel onto the canvas
4. Click any element to edit its properties in the right panel
5. Click **💾 Save Theme** to export a `.ds916theme` file

### Step 7 — Load the Theme

1. Right-click the DS916 tray icon
2. Click **📂 Load Theme…**
3. Select your `.ds916theme` file
4. The screen should immediately start displaying your theme

---

## Tray App — Right-Click Menu

| Option | Description |
|--------|-------------|
| **▶ Start Display** | Start streaming the current theme to the screen |
| **⏹ Stop Display** | Stop streaming (screen returns to splash screen) |
| **📂 Load Theme…** | Load a `.ds916theme` file |
| **🎨 Open Theme Builder** | Opens `theme_builder.html` in your default browser |
| **🔍 Discover Sensors** | Re-scan HWiNFO64 and update `ds916sensors.json` |
| **ℹ Status…** | Shows current status, sensor source, live sensor values |
| **⚙ Settings…** | Configure COM port, FPS, HWiNFO source, sensor map |
| **🗑 Uninstall…** | Remove from Windows startup and delete app data |
| **❌ Exit** | Close the tray app |

---

## Settings

Open via tray icon → **⚙ Settings…**

### General Tab

| Setting | Description |
|---------|-------------|
| **COM Port** | The serial port the DS916 is on (default: COM3) |
| **FPS** | Frames per second to stream (default: 6, max: 30) |
| **HWiNFO Source** | `sharedmem` for richer data, `registry` for no time limit |
| **Theme File** | Path to the last loaded theme (auto-remembered) |
| **Start with Windows** | Adds/removes the app from Windows startup |

### HWiNFO Tab

Configure the **11.5-hour auto-restart workaround** for HWiNFO64 free edition's shared memory time limit:

1. Set the path to `HWiNFO64.exe` (click **Detect** to find it automatically)
2. Click **Install Restart Task** to create a Windows Scheduled Task that silently restarts HWiNFO64 every 11.5 hours
3. Click **Remove Task** to disable it

### Sensor Map Tab

Maps sensor keys (like `CPU_USAGE`) to their HWiNFO shared memory index. These are populated automatically by **Discover Sensors** — you should rarely need to edit them manually.

---

## Theme Builder

Open `theme_builder.html` in Chrome or Edge.

### Layout

| Area | Description |
|------|-------------|
| **Left panel** | Element palette — drag or click to add to canvas |
| **Center** | Canvas — drag to move, resize handle to resize |
| **Right panel** | Properties for the selected element |
| **Bottom** | Layers panel — visibility toggle, z-order, delete |
| **Top bar** | Orientation, zoom, background, export |

### Adding Elements

**Drag** an element from the left panel onto the canvas, or **click** it to place at a default position.

**Preset elements** (marked ⊞) drop a label + value + bar group in one click — great for quickly building a sensor display.

### Element Types

| Type | Description |
|------|-------------|
| **Clock** | Live time — 12h or 24h, with or without seconds |
| **Date** | Live date — fully configurable format (DD-MM-YYYY, MM/DD/YYYY, etc.) |
| **Day of Week** | Full (Tuesday) or short (Tue) |
| **Sensor Value** | Live sensor reading with optional prefix and unit suffix |
| **Static Label** | Fixed text — double-click to edit |
| **Bar** | Horizontal progress bar bound to a sensor |
| **Ring Gauge** | Circular gauge bound to a sensor |
| **Line Graph** | Scrolling history graph with configurable window |
| **Rectangle** | Decorative divider or block |
| **Image** | PNG/JPG overlay layer |

### Resizing

- **Text elements** — drag the resize handle (bottom-right corner) to scale font size
- **Bars, graphs, rings** — drag the resize handle to change width/height
- **Arrow keys** — nudge selected element 1px; hold Shift for 10px

### Fonts

The font section in the Properties panel has two buttons:

- **🔍 System Fonts** — loads all fonts installed on your PC into the dropdown (Chrome/Edge only)
- **📁 Font File…** — load a `.ttf` or `.otf` file directly; it's embedded in the theme on export so it works on any PC

### Sensor Palette

By default only the most common sensors are shown (Time, CPU, GPU, Fans). Use the **＋ Sensors** button to open the full sensor picker with checkboxes. Import a `ds916sensors.json` file (from the **🔍 Sensors** button) to see all sensors specific to your hardware.

### Colors

Each color field has:
- A **color picker** for RGB selection
- An **alpha (0-255)** field for transparency
- A **+** button to save the color to the theme palette
- **Palette swatches** showing saved colors — click any to apply

### Export & Import

- **💾 Save Theme** — exports a single `.ds916theme` file with all assets (background image, custom fonts, image layers) embedded as base64
- **📂 Import** — loads a `.ds916theme` file (or legacy `.zip` format)

---

## Sensor Discovery

When the tray app starts with HWiNFO64 shared memory enabled, it automatically scans all available sensors and saves them to:

```
%APPDATA%\Roaming\DS916Tray\ds916sensors.json
```

This file contains every sensor HWiNFO64 exposes — including hardware-specific ones like additional fan headers, liquid cooling temps, per-core data, etc.

**In the Theme Builder**, click **🔍 Sensors** to import this file. All discovered sensors will appear in the element palette, grouped by type, and can be used in any element.

**Re-run discovery** at any time via tray icon → **🔍 Discover Sensors** (e.g. after adding new hardware or updating HWiNFO64).

> Sensor indices can vary between systems and HWiNFO versions. The tray app matches sensors by name (`"Total CPU Usage"`, `"CPU (Tctl/Tdie)"` etc.) rather than index, so they stay correct even if the order changes.

---

## HWiNFO64 Shared Memory — 12-Hour Limit

HWiNFO64 free edition disables shared memory after 12 hours. The tray app falls back to the registry (VSB Gadget) method automatically, but this provides fewer sensors.

**Workaround options:**

1. **Auto-restart task** (recommended) — use Settings → HWiNFO tab to install a Windows Scheduled Task that restarts HWiNFO64 every 11.5 hours silently
2. **Manual restart** — restart HWiNFO64 manually every 12 hours
3. **Use registry mode** — change HWiNFO Source to `registry` in Settings; no time limit but requires manually adding sensors to the HWiNFO Gadget

Check the current source any time via tray icon → **ℹ Status…**

---

## How JONSBO-AIO Works (Internals)

Based on our reverse engineering:

1. Reads a **`Setting.txt`** layout file from the theme folder defining element positions, sensor bindings, fonts, and colors
2. Reads hardware sensor values via Windows APIs at a configurable polling interval (~10 seconds for some sensors, causing occasional display stutter)
3. Composites all elements onto a canvas using **SkiaSharp**
4. Encodes the result as a JPEG using **libjpeg-turbo**
5. Sends the JPEG to the screen over COM3 using the protocol above, continuously at ~6fps

The `MSDISPLAYSDKWRRAPER.dll` is the PC-side SDK wrapper provided by ArtInChip. The device-side firmware runs on their **Luban-Lite** RTOS with CherryUSB.

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
  "visibleSensors": ["CLOCK", "DATE", "CPU_USAGE", "CPU_TEMP"],
  "customFonts": [{"family": "MyFont", "filename": "MyFont.ttf", "data": "data:font/ttf;base64,..."}],
  "elements": [
    {
      "id": "el_1",
      "type": "clock",
      "x": 10, "y": 20,
      "w": 440, "h": 80,
      "z": 10,
      "visible": true,
      "clockFormat": "12h",
      "clockSeconds": true,
      "fontSize": 72,
      "fontFamily": "Consolas",
      "bold": true,
      "color": "#ffffffff",
      "align": "center",
      "manualSize": false
    }
  ]
}
```

### Element Types

| Type | Description | Key properties |
|------|-------------|----------------|
| `clock` | Live time | `clockFormat` (12h/24h), `clockSeconds` |
| `date` | Live date | `dateFormat` (DD-MM-YYYY etc.) |
| `weekday` | Day of week | `weekdayFormat` (full/short) |
| `text` | Sensor value | `sensorKey`, `prefix`, `unit`, `manualSize` |
| `static` | Fixed label | `customText`, `manualSize` |
| `bar` | Progress bar | `sensorKey`, `maxValue`, `fillColor`, `bgColor` |
| `ring` | Ring gauge | `sensorKey`, `maxValue`, `arcColor`, `trackColor`, `ringWidth` |
| `linegraph` | Scrolling graph | `sensorKey`, `maxValue`, `lineColor`, `historySeconds` |
| `rect` | Rectangle | `fillColor`, `cornerRadius` |
| `image` | Image overlay | `data` (base64 data URL) |

> **`manualSize`**: when `false` (default for text), the selection box auto-sizes to content. Dragging the resize handle on text scales font size; on other elements it resizes the widget.

### Sensor Keys

Standard keys used in themes. Indices are mapped automatically from `ds916sensors.json`:

| Key | Description |
|-----|-------------|
| `CPU_USAGE` | CPU utilisation % |
| `CPU_TEMP` | CPU temperature |
| `CPU_FAN` | CPU fan RPM |
| `GPU_USAGE` | GPU core load % |
| `GPU_TEMP` | GPU temperature |
| `GPU_FAN1` / `GPU_FAN2` | GPU fan RPM |
| `MB_TEMP` | Motherboard temperature |
| `CHASSIS_FAN1` / `CHASSIS_FAN2` | Chassis fan RPM |
| `RAM_USAGE` | RAM utilisation % |
| `RAM_USED_GB` | RAM used in GB |
| `VRAM_USAGE` | VRAM utilisation % |
| `NET_DOWN` / `NET_UP` | Network speed |
| `CUSTOM_N` | Any sensor discovered by index N |

---

## Uninstalling

Right-click the tray icon → **🗑 Uninstall…**

This will:
1. Remove DS916Tray from Windows startup
2. Delete `%APPDATA%\Roaming\DS916Tray\` (config and sensor data)
3. Close the app

Then manually delete `DS916Tray.exe` and `theme_builder.html` from wherever you placed them.

---

## Compatible Devices

Confirmed working:
- **Jonsbo DS916** ✅

Likely compatible (same ArtInChip chip family, VID `0x33C3`):
- Other Jonsbo AIO LCD panels

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
