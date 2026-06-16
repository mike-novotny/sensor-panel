# DS916 Sensor Panel

A fully open-source sensor panel system for the **Jonsbo DS916** (and compatible ArtInChip-based USB LCD screens), built by reverse-engineering the device's USB protocol from scratch.

Design custom themes visually, stream live hardware sensor data from HWiNFO64, and run everything silently in the background — no proprietary software required.

![DS916 running a custom sensor panel](docs/preview.jpg)

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
- The baud rate setting is irrelevant — the device operates at USB 2.0 speeds regardless of what baud rate is configured on the virtual COM port
- The device accepts any standard baud rate value without error
- Only bytes 0, 8-11, and 56-59 need to be set correctly; all varying/timestamp fields can be zeroed
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
    header[1]  = 0x3C          # header length
    header[5]  = 0x06          # channel
    header[17] = 0x4F          # magic 'O'
    header[18] = 0x54          # magic 'T'
    header[19] = 0x06          # magic \x06
    header[24] = 0x1B
    header[33] = 0x1B
    header[39:44] = bytes([0x89, 0xB3, 0xFF, 0xFF, 0x00])
    header[44:56] = bytes([0x00,0x00,0x00,0x09,0x00,0x00,0x01,0x00,0x03,0x00,0x02,0x03])
    struct.pack_into('<I', header, 56, len(jpeg_bytes))
    struct.pack_into('<I', header,  8, len(jpeg_bytes) + 60)
    return bytes(header) + jpeg_bytes

# Create a test image
img = Image.new('RGB', (462, 1920), color=(20, 20, 40))
buf = io.BytesIO()
img.save(buf, format='JPEG', quality=88)
jpeg = buf.getvalue()

# Send to screen (close JONSBO-AIO first!)
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
├── theme_builder.html    # Visual theme designer (open in any browser)
├── ds916_tray.py         # Background renderer + system tray app
├── build.bat             # One-click build script (creates DS916Tray.exe)
└── README.md
```

---

## Features

### Theme Builder (`theme_builder.html`)
- **Visual drag-and-drop editor** — same look and feel as the Jonsbo theme builder
- **Element types:**
  - Clock (12h/24h, with/without seconds)
  - Date (fully configurable format: DD-MM-YYYY, MM/DD/YYYY, MMMM DD YYYY, etc.)
  - Day of Week (full or abbreviated)
  - Sensor value text (with prefix/suffix)
  - Horizontal progress bar
  - Ring gauge
  - Line graph (scrolling history)
  - Static label
  - Rectangle / divider
  - Image layer
- **Sensors supported:** CPU usage/temp/fan, GPU usage/temp/fans, RAM usage, motherboard temp, chassis fans, network up/down
- **Color picker** with alpha channel + saved palette swatches per theme
- **Snap-to-grid** and alignment tools (center, edge snap, element-to-element align)
- **Layers panel** with visibility toggle and z-order control
- **Background support:** PNG, JPG, GIF, MP4, AVI, WMV
- **Import/Export** as `.zip` (theme JSON + all assets bundled)
- Works entirely in the browser — no install required

### Tray Renderer (`ds916_tray.py` / `DS916Tray.exe`)
- Runs silently in the Windows **system tray**
- **Auto-starts with Windows** and loads the last used theme automatically
- Right-click tray menu: Start/Stop display, Load Theme, Open Builder, Settings, Exit
- **HWiNFO64 integration** via Windows Registry (VSB gadget mode) — no usage limit
- Optional **HWiNFO64 shared memory** mode for richer sensor access (requires HWiNFO Pro or restart every 12h on free version)
- Auto-fallback: tries shared memory first, falls back to registry
- **Sensor mapping UI** — configure which HWiNFO registry index maps to which sensor key
- **Line graph history** buffers maintained per-element across frames
- Configurable COM port, FPS, and JPEG quality

---

## Requirements

### Theme Builder
- Any modern browser (Chrome, Edge, Firefox)
- No install required — just open `theme_builder.html`

### Tray Renderer (running from source)
```bash
pip install pillow pyserial pystray
```
- Python 3.9+
- Windows 10/11
- HWiNFO64 running with Gadget mode enabled

### Tray Renderer (compiled exe)
- No Python required
- Run `build.bat` once to compile

---

## Setup

### 1. HWiNFO64 Configuration

1. Download and install [HWiNFO64](https://www.hwinfo.com/download/) (free)
2. Launch in **Sensors-only** mode
3. Open **Settings → HWiNFO Gadget** tab
4. Enable the gadget and add the sensors you want to display
5. Note the index number of each sensor (they appear as `ValueRaw0`, `ValueRaw1`, etc. in the registry)

To see your sensor indices:
```powershell
Get-ItemProperty "HKCU:\SOFTWARE\HWiNFO64\VSB"
```

### 2. Configure Sensor Mapping

When you first run `DS916Tray.exe`, right-click the tray icon → **Settings → Sensor Map** and enter the registry index for each sensor based on your HWiNFO64 output.

Example mapping (will vary by system):
```
CPU_USAGE    → 0   (Total CPU Usage)
CPU_TEMP     → 1   (CPU Tctl/Tdie)
MB_TEMP      → 2   (Motherboard)
CPU_FAN      → 3   (CPU1 fan)
CHASSIS_FAN1 → 4   (Chassis1)
CHASSIS_FAN2 → 5   (Chassis2)
GPU_TEMP     → 6   (GPU Temperature)
GPU_FAN1     → 7   (GPU Fan1)
GPU_FAN2     → 8   (GPU Fan2)
GPU_USAGE    → 9   (GPU Core Load)
```

### 3. Design a Theme

1. Open `theme_builder.html` in your browser
2. Drag elements from the left panel onto the canvas
3. Click any element to edit its properties in the right panel
4. Use the **⚙ Sensor Map** button to store your index mapping in the theme
5. Click **💾 Export** to save as a `.zip` file

### 4. Run the Display

**From source:**
```bash
python ds916_tray.py
```

**As compiled exe:**
1. Run `build.bat` once — this installs dependencies and builds `dist/DS916Tray.exe`
2. Copy `theme_builder.html` into the same `dist/` folder
3. Run `DS916Tray.exe` — it sits in your system tray and adds itself to Windows startup
4. Right-click tray icon → **Load Theme** → select your exported `.zip`

> **Important:** Close JONSBO-AIO completely (including system tray) before starting the renderer — both cannot hold COM3 open at the same time.

---

## Building the Exe

```bat
build.bat
```

This runs:
```bash
pip install pyinstaller pillow pyserial pystray
pyinstaller --onefile --windowed --name=DS916Tray ds916_tray.py
```

Output: `dist/DS916Tray.exe` (~15-20MB standalone executable, no Python required)

---

## Compatible Devices

This protocol was discovered on the **Jonsbo DS916** but may work on other ArtInChip-based USB LCD panels. The chip vendor (ArtInChip, VID `0x33C3`) makes display SoCs used in various PC case LCD accessories.

Known compatible devices (community-reported):
- Jonsbo DS916 ✅ (confirmed)

If you get this working on another device, please open an issue or PR to update this list.

---

## How JONSBO-AIO Works (Internals)

Based on our reverse engineering:

1. JONSBO-AIO reads a **`Setting.txt`** layout file from the theme folder, which defines element positions, sensor bindings, fonts, and colors using a custom key-value format
2. It reads hardware sensor values via Windows APIs at a configurable polling interval (~10 seconds for some sensors, which causes the occasional display stutter)
3. It composites all elements onto a canvas using **SkiaSharp** (the `libSkiaSharp.dll` in the install folder)
4. It encodes the result as a JPEG using **libjpeg-turbo** (the `turbojpeg.dll`)
5. It sends the JPEG to the screen over COM3 using the protocol documented above, continuously at ~6fps

The `MSDISPLAYSDKWRRAPER.dll` is the PC-side SDK wrapper provided by ArtInChip. The device-side firmware runs on their **Luban-Lite** RTOS SDK with CherryUSB.

---

## Theme File Format

Themes are exported as `.zip` files containing:

```
ThemeName.zip
├── ThemeName.ds916theme    # JSON theme definition
├── back.png / back.mp4     # Background image or video (optional)
└── layer_name.png          # Any image layers (optional)
```

The `.ds916theme` JSON format:
```json
{
  "name": "MyTheme",
  "width": 462,
  "height": 1920,
  "background": "#111114ff",
  "sensorMap": {
    "CPU_USAGE": 0,
    "CPU_TEMP": 1,
    "GPU_USAGE": 9
  },
  "themeColors": ["#00b4ffff", "#ff0000ff"],
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
      "align": "center"
    }
  ]
}
```

### Element Types

| Type | Description | Key properties |
|------|-------------|----------------|
| `clock` | Live time display | `clockFormat` (12h/24h), `clockSeconds` |
| `date` | Live date display | `dateFormat` (DD-MM-YYYY etc.) |
| `weekday` | Day of week | `weekdayFormat` (full/short) |
| `text` | Sensor value | `sensorKey`, `prefix`, `unit` |
| `static` | Fixed label | `customText` |
| `bar` | Progress bar | `sensorKey`, `maxValue`, `fillColor`, `bgColor` |
| `ring` | Ring gauge | `sensorKey`, `maxValue`, `arcColor`, `trackColor`, `ringWidth` |
| `linegraph` | Scrolling graph | `sensorKey`, `maxValue`, `lineColor`, `historySeconds` |
| `rect` | Rectangle | `fillColor`, `cornerRadius` |
| `image` | Image overlay | `filename` |

### Sensor Keys

| Key | Description |
|-----|-------------|
| `CPU_USAGE` | CPU utilisation % |
| `CPU_TEMP` | CPU temperature °C |
| `CPU_FAN` | CPU fan speed RPM |
| `GPU_USAGE` | GPU core load % |
| `GPU_TEMP` | GPU temperature °C |
| `GPU_FAN1` | GPU fan 1 RPM |
| `GPU_FAN2` | GPU fan 2 RPM |
| `MB_TEMP` | Motherboard temperature °C |
| `CHASSIS_FAN1` | Chassis fan 1 RPM |
| `CHASSIS_FAN2` | Chassis fan 2 RPM |
| `RAM_USAGE` | RAM utilisation % |
| `RAM_USED_GB` | RAM used in GB |
| `NET_DOWN` | Network download speed |
| `NET_UP` | Network upload speed |

---

## Contributing

Pull requests welcome. Areas that would benefit most from community help:

- **More compatible devices** — test on other ArtInChip-based LCD panels and report results
- **Shared memory reader** — a proper HWiNFO64 shared memory implementation for richer sensor data without the 12h limit workaround
- **More sensors** — disk temp, VRAM usage, GPU power, per-core CPU usage
- **Theme gallery** — share your `.zip` themes in the Discussions tab
- **Linux/Mac support** — the protocol is the same, only the HWiNFO integration would need replacing (e.g. with `lm-sensors`)

---

## License

MIT — do whatever you want with it. If you build something cool, share it!

---

## Acknowledgements

Protocol reverse-engineered using Wireshark + USBPcap on Windows 11.

ArtInChip Luban-Lite SDK: https://github.com/artinchip/luban-lite  
CherryUSB: https://github.com/cherry-embedded/CherryUSB  
HWiNFO64: https://www.hwinfo.com
