"""
DS916 Tray Renderer
Runs in the Windows system tray, streams themes to the DS916 screen.
Compile with: pyinstaller --onefile --windowed --icon=icon.ico --name=DS916Tray ds916_tray.py
"""
import sys, os, json, struct, time, io, threading, winreg, ctypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import serial
import serial.tools.list_ports
import pystray
from pystray import MenuItem as Item
import PIL.Image as PILImage

# ── Paths ────────────────────────────────────────────────────────────────────
APP_NAME    = 'DS916Tray'
CONFIG_DIR  = os.path.join(os.environ.get('APPDATA',''), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
os.makedirs(CONFIG_DIR, exist_ok=True)

# ── Default config ────────────────────────────────────────────────────────────
DEFAULT_CFG = {
    'com_port':   'COM3',
    'fps':        6,
    'theme_path': '',
    'autostart':  True,
    'hwinfo_path': '',
    'hwinfo_source': 'sharedmem',   # sharedmem (richer) with registry fallback
    'sensor_map': {
        'CPU_USAGE':   46,
        'CPU_TEMP':    94,
        'MB_TEMP':    139,
        'CPU_FAN':    162,
        'CHASSIS_FAN1':163,
        'CHASSIS_FAN2':164,
        'GPU_TEMP':   194,
        'GPU_FAN1':   204,
        'GPU_FAN2':   205,
        'GPU_USAGE':  224,
        'VRAM_USAGE': 228,
        'RAM_USAGE':    5,
        'RAM_USED_GB':  3,
        'NET_DOWN':   302,
        'NET_UP':     303
    }
}

def load_cfg():
    try:
        with open(CONFIG_FILE) as f: c=json.load(f)
        # Merge with defaults for any missing keys
        for k,v in DEFAULT_CFG.items():
            if k not in c: c[k]=v
        if 'sensor_map' in c:
            for k,v in DEFAULT_CFG['sensor_map'].items():
                if k not in c['sensor_map']: c['sensor_map'][k]=v
        return c
    except: return dict(DEFAULT_CFG)

def save_cfg(cfg):
    with open(CONFIG_FILE,'w') as f: json.dump(cfg,f,indent=2)

cfg = load_cfg()

# ── DS916 Protocol ────────────────────────────────────────────────────────────
HEADER_TPL = bytearray([
    0x00,0x3c,0x00,0x00,0x00,0x06,0x00,0x00,
    0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,
    0x4f,0x54,0x06,
    0x00,0x00,0x00,0x00,
    0x1b,
    0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,
    0x1b,0x00,
    0x00,0x00,0x00,0x00,
    0x89,0xb3,0xff,0xff,0x00,
    0x00,0x00,0x00,0x09,0x00,0x00,
    0x01,0x00,0x03,0x00,0x02,0x03,
    0x00,0x00,0x00,0x00,
])

def make_frame(jpeg, first=False):
    h = bytearray(HEADER_TPL)
    h[0] = 0x03 if first else 0x00
    struct.pack_into('<I', h, 56, len(jpeg))
    struct.pack_into('<I', h,  8, len(jpeg)+60)
    return bytes(h)+jpeg

# ── HWiNFO Reader ─────────────────────────────────────────────────────────────
_shm_handle = None
_shm_data   = None

def try_open_sharedmem():
    """Try to open HWiNFO shared memory using pure ctypes."""
    global _shm_handle, _shm_data
    print('Attempting to open HWiNFO shared memory...')
    try:
        import ctypes

        kernel32      = ctypes.windll.kernel32
        FILE_MAP_READ = 0x0004

        # Set correct return types — default c_int truncates 64-bit pointers
        kernel32.OpenFileMappingW.restype  = ctypes.c_void_p
        kernel32.MapViewOfFile.restype     = ctypes.c_void_p
        kernel32.UnmapViewOfFile.argtypes  = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes      = [ctypes.c_void_p]

        # Open the named file mapping
        win_handle = None
        for name in ('Global\\HWiNFO_SENS_SM2', 'HWiNFO_SENS_SM2'):
            h = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
            if h:
                win_handle = h
                print(f'  Opened mapping: "{name}" handle={h}')
                break

        if not win_handle:
            raise OSError(
                'OpenFileMappingW failed — HWiNFO64 not running or '
                'Shared Memory Support not enabled.\n'
                '  In HWiNFO64: Settings → General → Shared Memory Support')

        # Map into our address space
        SM_SIZE = 1 * 1024 * 1024  # 1MB
        ptr = kernel32.MapViewOfFile(win_handle, FILE_MAP_READ, 0, 0, SM_SIZE)
        if not ptr:
            kernel32.CloseHandle(win_handle)
            raise OSError(f'MapViewOfFile failed (error {kernel32.GetLastError()})')
        print(f'  MapViewOfFile ptr=0x{ptr:X}')

        # Read using ctypes.string_at — this is the correct way to read
        # from a raw memory address in Python on Windows
        sig_bytes = ctypes.string_at(ptr, 4)
        sig = struct.unpack('<I', sig_bytes)[0]
        print(f'  Signature: 0x{sig:08X} (WIFH=0x57494648, SiWH=0x53695748)')

        VALID_SIGS = {0x57494648, 0x53695748}
        if sig not in VALID_SIGS:
            kernel32.UnmapViewOfFile(ptr)
            kernel32.CloseHandle(win_handle)
            raise OSError(f'Unknown signature 0x{sig:08X} — HWiNFO still loading?')

        # Store both so we can read later and keep alive
        # Decode layout using reverse-engineered struct (github.com/namazso/hwinfosharedmem.h)
        hdr   = ctypes.string_at(ptr, 48)
        off_e = struct.unpack_from('<I', hdr, 0x20)[0]
        sz_e  = struct.unpack_from('<I', hdr, 0x24)[0]
        n_e   = struct.unpack_from('<I', hdr, 0x28)[0]
        print(f'  Entries: {n_e} @ offset {off_e}, {sz_e} bytes each')
        # Store 7-tuple: kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e
        _shm_handle = (kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e)
        _shm_data   = True
        print('  Shared memory OK ✅')
        return True

    except Exception as e:
        print(f'  Shared memory failed: {e}')
        _shm_handle = None
        _shm_data   = None
        return False


def read_sharedmem(sensor_map):
    """Read sensor values from HWiNFO shared memory.
    Layout from: github.com/namazso/hwinfosharedmem.h
    HWiNFOEntry: type(4) sensor_index(4) id(4) name_orig(128) name_user(128) unit(16) value(8d)
    Value is a double at offset 0x11C = 284 within each entry.
    """
    data = {}
    if not _shm_handle: return data
    try:
        import ctypes
        kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e = _shm_handle
        ptr = int(ptr)
        if not ptr or sz_e < 285 or n_e <= 0: return data

        VALUE_OFFSET = 0x11C  # 284 — double at this offset within HWiNFOEntry

        idx_to_key = {v: k for k, v in sensor_map.items() if v is not None}
        for idx, key in idx_to_key.items():
            if idx >= n_e: continue
            entry_ptr = ptr + off_e + idx * sz_e
            val_bytes = ctypes.string_at(entry_ptr + VALUE_OFFSET, 8)
            val = struct.unpack('<d', val_bytes)[0]
            data[key] = val

    except Exception as e:
        print(f'Shared memory read error: {e}')
    return data


def discover_sensors():
    """Scan all HWiNFO shared memory entries and save to ds916sensors.json.
    Called automatically on startup and available from tray menu."""
    if not _shm_handle:
        print('Cannot discover sensors — shared memory not available')
        return False
    try:
        import ctypes
        kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e = _shm_handle
        ptr = int(ptr)

        TYPE_NAMES = {0:'Other',1:'Temperature',2:'Voltage',3:'Fan',
                      4:'Current',5:'Power',6:'Clock',7:'Usage',8:'Other'}

        sensors = []
        for i in range(n_e):
            entry = ctypes.string_at(ptr + off_e + i * sz_e, sz_e)
            stype     = struct.unpack_from('<I', entry, 0x00)[0]
            name_orig = entry[0x0C:0x0C+128].rstrip(b'\x00').decode('ascii','replace').strip()
            unit      = entry[0x10C:0x10C+16].rstrip(b'\x00').decode('ascii','replace').strip()
            val       = struct.unpack_from('<d', entry, 0x11C)[0]
            if not name_orig: continue
            sensors.append({
                'index':  i,
                'type':   TYPE_NAMES.get(stype, 'Other'),
                'name':   name_orig,
                'unit':   unit,
                'sample': round(val, 3),
            })

        out = {'generated': str(datetime.now()), 'sensors': sensors}
        sensors_path = os.path.join(CONFIG_DIR, 'ds916sensors.json')
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(sensors_path, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2)
        print(f'Sensor discovery: {len(sensors)} sensors saved to {sensors_path}')
        return sensors_path
    except Exception as e:
        print(f'Sensor discovery error: {e}')
        return False


    """Read sensor values from HWiNFO64 VSB registry. Returns {KEY: value}."""
    data = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'SOFTWARE\HWiNFO64\VSB')
        for skey, idx in sensor_map.items():
            if idx is None: continue
            try:
                raw = winreg.QueryValueEx(key, f'ValueRaw{idx}')[0]
                data[skey] = float(raw)
            except: pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f'Registry read error: {e}')
    return data

def read_sensors():
    """Read all sensor values using best available method."""
    sm  = cfg.get('sensor_map', DEFAULT_CFG['sensor_map'])
    src = cfg.get('hwinfo_source', 'sharedmem')
    if src == 'sharedmem':
        if _shm_handle is None:
            try_open_sharedmem()
        if _shm_handle is not None:
            d = read_sharedmem(sm)
            if d: return d
    return read_registry(sm)

# ── Font loader ───────────────────────────────────────────────────────────────
_font_cache = {}
_custom_font_files = {}  # family name -> temp file path (extracted from theme)

def _find_windows_font(family, bold=False):
    """Search Windows font directories for a font matching the family name."""
    import glob, re

    # Known exact mappings for common fonts
    KNOWN = {
        'Consolas':    ('consolab.ttf', 'consola.ttf'),
        'Arial':       ('arialbd.ttf',  'arial.ttf'),
        'Segoe UI':    ('segoeuib.ttf', 'segoeui.ttf'),
        'Courier New': ('courbd.ttf',   'cour.ttf'),
        'Tahoma':      ('tahomabd.ttf', 'tahoma.ttf'),
        'Verdana':     ('verdanab.ttf', 'verdana.ttf'),
        'Impact':      ('impact.ttf',   'impact.ttf'),
        'Georgia':     ('georgiab.ttf', 'georgia.ttf'),
        'Calibri':     ('calibrib.ttf', 'calibri.ttf'),
        'Times New Roman': ('timesbd.ttf', 'times.ttf'),
        'Comic Sans MS':   ('comicbd.ttf', 'comic.ttf'),
        'Trebuchet MS':    ('trebucbd.ttf','trebuc.ttf'),
        'Palatino Linotype':('palabd.ttf','pala.ttf'),
        'Century Gothic':  ('gothicb.ttf','gothic.ttf'),
    }

    font_dirs = [
        'C:/Windows/Fonts/',
        os.path.expanduser('~/AppData/Local/Microsoft/Windows/Fonts/'),
    ]

    # Try known mapping first
    if family in KNOWN:
        b, r = KNOWN[family]
        fname = b if bold else r
        for d in font_dirs:
            p = d + fname
            if os.path.exists(p): return p
        # Try the other variant
        fname2 = r if bold else b
        for d in font_dirs:
            p = d + fname2
            if os.path.exists(p): return p

    # Dynamic search: look for files matching the family name
    safe = re.sub(r'[^a-z0-9]', '', family.lower())
    for d in font_dirs:
        candidates = glob.glob(d + '*.ttf') + glob.glob(d + '*.otf')
        scored = []
        for path in candidates:
            base = re.sub(r'[^a-z0-9]', '', os.path.basename(path).lower())
            if safe in base:
                # Prefer bold variants when bold=True
                is_bold = any(x in base for x in ['bold','bd','b'])
                score = (is_bold == bold) * 2 + (safe == base.replace('.ttf','').replace('.otf',''))
                scored.append((score, path))
        if scored:
            scored.sort(reverse=True)
            return scored[0][1]

    return None

def get_font(family='Consolas', size=32, bold=False):
    key = (family, size, bold)
    if key in _font_cache: return _font_cache[key]

    font = None

    # 1. Try custom embedded font first (extracted from theme)
    if family in _custom_font_files:
        try:
            font = ImageFont.truetype(_custom_font_files[family], size)
        except Exception as e:
            print(f'Custom font load error ({family}): {e}')

    # 2. Search Windows Fonts
    if not font:
        path = _find_windows_font(family, bold)
        if path:
            try:
                font = ImageFont.truetype(path, size)
            except Exception as e:
                print(f'Font load error ({path}): {e}')

    # 3. Fallback to Consolas
    if not font:
        try:
            fb = 'C:/Windows/Fonts/' + ('consolab.ttf' if bold else 'consola.ttf')
            font = ImageFont.truetype(fb, size)
        except:
            font = ImageFont.load_default()

    if family not in _font_cache or font:
        _font_cache[key] = font
    return font

def load_custom_fonts_from_theme(theme):
    """Extract embedded font data URLs from theme and write to temp files."""
    import tempfile, base64
    for cf in theme.get('customFonts', []):
        family = cf.get('family','')
        data_url = cf.get('data','')
        filename = cf.get('filename','font.ttf')
        if not family or not data_url or ',' not in data_url:
            continue
        try:
            _, b64 = data_url.split(',', 1)
            font_bytes = base64.b64decode(b64)
            ext = os.path.splitext(filename)[1] or '.ttf'
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(font_bytes)
            tmp.close()
            _custom_font_files[family] = tmp.name
            # Clear cache entries for this family so they get reloaded
            for k in list(_font_cache.keys()):
                if k[0] == family: del _font_cache[k]
            print(f'Custom font extracted: "{family}" -> {tmp.name}')
        except Exception as e:
            print(f'Custom font extract error ({family}): {e}')

# ── Colour helpers ────────────────────────────────────────────────────────────
def parse_color(c):
    """Parse #rrggbbaa (builder format) → (r,g,b,a) tuple."""
    if not c or len(c)<7: return (255,255,255,255)
    c=c.lstrip('#')
    if len(c)==6:  return (int(c[0:2],16),int(c[2:4],16),int(c[4:6],16),255)
    if len(c)==8:  return (int(c[0:2],16),int(c[2:4],16),int(c[4:6],16),int(c[6:8],16))
    return (255,255,255,255)

def color_rgb(c):
    r,g,b,a = parse_color(c)
    return (r,g,b)

def color_rgba(c):
    return parse_color(c)

# ── Graph history buffers ──────────────────────────────────────────────────────
_graph_history = {}  # elem_id -> deque of values

def graph_push(eid, value, maxlen=120):
    from collections import deque
    if eid not in _graph_history:
        _graph_history[eid] = deque(maxlen=maxlen)
    _graph_history[eid].append(value)

def graph_get(eid):
    return list(_graph_history.get(eid, []))

# ── Renderer ──────────────────────────────────────────────────────────────────
def render_frame(theme, sensors):
    W = theme.get('width', 462)
    H = theme.get('height', 1920)
    bg = color_rgb(theme.get('background','#111114ff'))
    img = Image.new('RGB', (W, H), bg)

    # Composite background image if present
    bg_pil = theme.get('_background_image')
    if bg_pil:
        try:
            resized = bg_pil.resize((W, H), Image.LANCZOS).convert('RGB')
            img.paste(resized, (0, 0))
        except Exception as e:
            print(f'Background image error: {e}')

    draw = ImageDraw.Draw(img, 'RGBA')
    now = datetime.now()

    def sv(key, default=0):
        return sensors.get(key, default)

    # Sort by z ascending (lower z = further back)
    elements = sorted(theme.get('elements',[]), key=lambda e: e.get('z',0))

    for el in elements:
        if not el.get('visible', True): continue
        x   = int(el.get('x', 0))
        y   = int(el.get('y', 0))
        w   = int(el.get('w', 100))
        h   = int(el.get('h', 30))
        typ = el.get('type','')

        if typ in ('text','static','clock','date','weekday'):
            fs    = int(el.get('fontSize', 32))
            fam   = el.get('fontFamily', 'Consolas')
            bold  = el.get('bold', False)
            color = color_rgb(el.get('color','#ffffffff'))
            align = el.get('align','left')
            pre   = el.get('prefix','')
            unit  = el.get('unit','')

            if typ=='clock':
                fmt = el.get('clockFormat','12h')
                secs = el.get('clockSeconds', True)
                if fmt=='12h':
                    text = now.strftime('%I:%M:%S %p' if secs else '%I:%M %p').lstrip('0')
                else:
                    text = now.strftime('%H:%M:%S' if secs else '%H:%M')
            elif typ=='date':
                dfmt = el.get('dateFormat','DD-MM-YYYY')
                text = dfmt.replace('YYYY',now.strftime('%Y'))\
                           .replace('YY',now.strftime('%y'))\
                           .replace('MMMM',now.strftime('%B'))\
                           .replace('MMM',now.strftime('%b'))\
                           .replace('MM',now.strftime('%m'))\
                           .replace('DD',now.strftime('%d'))\
                           .replace('D',str(now.day))
            elif typ=='weekday':
                wfmt = el.get('weekdayFormat','full')
                text = now.strftime('%A') if wfmt=='full' else now.strftime('%a')
            elif typ=='static':
                text = el.get('customText','Label')
            else:  # sensor value text
                raw  = sv(el.get('sensorKey',''), 0)
                unit = el.get('unit','')
                # Format based on unit: whole numbers for %, RPM, °C; 1dp for V, W, GB
                if isinstance(raw, float):
                    whole_units = {'%', 'RPM', '\u00B0C', 'C', 'MHz', 'W'}
                    if unit.strip() in whole_units or raw == int(raw):
                        val_str = str(int(round(raw)))
                    else:
                        val_str = f'{raw:.1f}'
                else:
                    val_str = str(raw)
                text = pre + val_str + (' ' if unit and val_str else '') + unit

            font = get_font(fam, fs, bold)
            # Compute text position based on alignment
            try:
                bbox = draw.textbbox((0,0), text, font=font)
                tw = bbox[2]-bbox[0]
            except: tw = fs*len(text)//2
            if align=='center':   tx = x + (w-tw)//2
            elif align=='right':  tx = x + w - tw
            else:                 tx = x
            draw.text((tx, y), text, font=font, fill=color)

        elif typ=='bar':
            raw_val = float(sv(el.get('sensorKey','CPU_USAGE'), 0))
            max_val = float(el.get('maxValue', 100))
            pct     = max(0.0, min(1.0, raw_val/max_val if max_val else 0))
            rad     = int(el.get('cornerRadius', 0))
            thick   = int(el.get('borderThickness', 0))
            bg_c    = color_rgba(el.get('bgColor','#1a1a2299'))
            fill_c  = color_rgba(el.get('fillColor','#00b4ffff'))
            bord_c  = color_rgba(el.get('borderColor','#00000000'))
            # Background
            if rad > 0:
                draw.rounded_rectangle([x,y,x+w,y+h], radius=rad, fill=bg_c)
            else:
                draw.rectangle([x,y,x+w,y+h], fill=bg_c)
            # Fill
            fw = int(w*pct)
            if fw > 0:
                if rad > 0:
                    draw.rounded_rectangle([x,y,x+fw,y+h], radius=rad, fill=fill_c)
                else:
                    draw.rectangle([x,y,x+fw,y+h], fill=fill_c)
            # Border
            if thick > 0:
                draw.rectangle([x,y,x+w,y+h], outline=bord_c, width=thick)

        elif typ=='ring':
            raw_val = float(sv(el.get('sensorKey','CPU_USAGE'), 0))
            max_val = float(el.get('maxValue', 100))
            pct     = max(0.0, min(1.0, raw_val/max_val if max_val else 0))
            rw      = int(el.get('ringWidth', 14))
            arc_c   = color_rgb(el.get('arcColor','#00b4ffff'))
            trk_c   = color_rgb(el.get('trackColor','#1a1a3399'))
            diam    = min(w, h)
            margin  = rw//2 + 2
            box     = [x+margin, y+margin, x+diam-margin, y+diam-margin]
            # Track
            draw.arc(box, 0, 360, fill=trk_c, width=rw)
            # Arc
            start_a = -90
            end_a   = start_a + int(360*pct)
            if pct > 0:
                draw.arc(box, start_a, end_a, fill=arc_c, width=rw)
            # Label
            if el.get('showLabel', True):
                lfs   = int(el.get('labelFontSize', 28))
                lfam  = el.get('labelFontFamily','Consolas')
                lbold = el.get('labelBold', True)
                lcolor= color_rgb(el.get('labelColor','#ffffffff'))
                unit  = el.get('unit','')
                label = f'{int(raw_val)}{unit}'
                lfont = get_font(lfam, lfs, lbold)
                try:
                    bbox = draw.textbbox((0,0), label, font=lfont)
                    tw,th = bbox[2]-bbox[0], bbox[3]-bbox[1]
                except: tw=th=lfs
                cx = x + diam//2 - tw//2
                cy = y + diam//2 - th//2
                draw.text((cx,cy), label, font=lfont, fill=lcolor)

        elif typ=='linegraph':
            raw_val = float(sv(el.get('sensorKey','CPU_USAGE'), 0))
            max_val = float(el.get('maxValue', 100))
            eid     = el.get('id','')
            hist_s  = int(el.get('historySeconds', 60))
            maxlen  = max(10, hist_s * cfg.get('fps',6))
            if eid not in _graph_history:
                from collections import deque
                _graph_history[eid] = deque(maxlen=maxlen)
            _graph_history[eid].append(raw_val)
            pts = list(_graph_history[eid])

            lw   = int(el.get('lineWidth',2))
            rad  = int(el.get('cornerRadius',4))
            bg_c = color_rgba(el.get('bgColor','#0a0a1499'))
            lc   = color_rgb(el.get('lineColor','#00b4ffff'))
            fc   = color_rgba(el.get('fillColor','#00b4ff33'))
            gc   = color_rgba(el.get('gridColor','#ffffff22'))
            show_grid = el.get('showGrid', True)

            if rad>0: draw.rounded_rectangle([x,y,x+w,y+h],radius=rad,fill=bg_c)
            else:     draw.rectangle([x,y,x+w,y+h],fill=bg_c)

            if show_grid:
                for gi in range(1,4):
                    gy = y + h*gi//4
                    draw.line([(x,gy),(x+w,gy)], fill=gc, width=1)

            if len(pts)>=2:
                n=len(pts)
                def gx(i): return x + int(i/(n-1)*w)
                def gy_v(v): return y+h - int(max(0,min(1,v/max_val if max_val else 0))*h*0.88+h*0.06)
                coords = [(gx(i), gy_v(v)) for i,v in enumerate(pts)]
                # Fill polygon
                poly = [(x,y+h)] + coords + [(x+w,y+h)]
                draw.polygon(poly, fill=fc)
                # Line
                draw.line(coords, fill=lc, width=lw)

        elif typ=='rect':
            fill_c = color_rgba(el.get('fillColor','#00b4ff99'))
            rad    = int(el.get('cornerRadius',0))
            if rad>0: draw.rounded_rectangle([x,y,x+w,y+h],radius=rad,fill=fill_c)
            else:     draw.rectangle([x,y,x+w,y+h],fill=fill_c)

        elif typ=='image':
            # Image layers are loaded at theme load time
            img_data = el.get('_pil_image')
            if img_data:
                try:
                    resized = img_data.resize((w,h), Image.LANCZOS)
                    img.paste(resized, (x,y))
                except: pass

    return img

# ── Load theme ─────────────────────────────────────────────────────────────────
_current_theme = None
_current_theme_path = ''

def load_theme(path):
    global _current_theme, _current_theme_path
    try:
        import zipfile, base64
        if path.endswith('.zip'):
            with zipfile.ZipFile(path) as z:
                tfile = next((n for n in z.namelist() if n.endswith('.ds916theme')), None)
                if not tfile: return False
                theme = json.loads(z.read(tfile).decode())
                # Load image layers from ZIP
                for el in theme.get('elements',[]):
                    if el.get('type')=='image' and el.get('filename'):
                        try:
                            data = z.read(el['filename'])
                            el['_pil_image'] = PILImage.open(io.BytesIO(data)).convert('RGBA')
                        except: pass
                # Load background from ZIP
                back = next((n for n in z.namelist() if n.lower().startswith('back.')), None)
                if back:
                    data = z.read(back)
                    theme['_background_image'] = PILImage.open(io.BytesIO(data)).convert('RGBA')
        elif path.endswith('.ds916theme'):
            with open(path, encoding='utf-8') as f: theme = json.load(f)
            # Background is embedded as a data URL: "data:image/png;base64,..."
            bg_data_url = theme.get('backgroundImage')
            if bg_data_url and isinstance(bg_data_url, str) and ',' in bg_data_url:
                try:
                    header, b64 = bg_data_url.split(',', 1)
                    img_bytes = base64.b64decode(b64)
                    theme['_background_image'] = PILImage.open(io.BytesIO(img_bytes)).convert('RGBA')
                    print(f'Background image loaded from embedded data ({len(img_bytes)//1024}KB)')
                except Exception as e:
                    print(f'Background image decode error: {e}')
            # Image layers are also embedded as data URLs
            for el in theme.get('elements', []):
                if el.get('type') == 'image' and el.get('data'):
                    try:
                        du = el['data']
                        if ',' in du:
                            _, b64 = du.split(',', 1)
                            img_bytes = base64.b64decode(b64)
                            el['_pil_image'] = PILImage.open(io.BytesIO(img_bytes)).convert('RGBA')
                    except Exception as e:
                        print(f'Image layer decode error: {e}')
        else:
            return False

        # Load sensor map — priority order:
        # 1. Discovered sensors file (most accurate for this system)
        # 2. Config sensor_map (user-configured)
        # 3. Theme's embedded sensorMap (least reliable — may have wrong indices)
        sensors_path = os.path.join(CONFIG_DIR, 'ds916sensors.json')
        if os.path.exists(sensors_path):
            try:
                with open(sensors_path, encoding='utf-8') as sf:
                    disc = json.load(sf)
                # Build name->index map from discovered sensors
                name_to_idx = {s['name']: s['index'] for s in disc.get('sensors',[])}
                # Canonical names for each sensor key
                KEY_NAMES = {
                    'CPU_USAGE':    ['Total CPU Usage'],
                    'CPU_TEMP':     ['CPU (Tctl/Tdie)', 'CPU Package', 'CPU Temperature'],
                    'MB_TEMP':      ['Motherboard'],
                    'CPU_FAN':      ['CPU1', 'CPU Fan', 'CPU_OPT'],
                    'CHASSIS_FAN1': ['Chassis1', 'Chassis Fan 1', 'CHA_FAN1'],
                    'CHASSIS_FAN2': ['Chassis2', 'Chassis Fan 2', 'CHA_FAN2'],
                    'CHASSIS_FAN3': ['Chassis3', 'Chassis Fan 3', 'CHA_FAN3'],
                    'GPU_TEMP':     ['GPU Temperature', 'GPU Temp'],
                    'GPU_FAN1':     ['GPU Fan1', 'GPU Fan 1'],
                    'GPU_FAN2':     ['GPU Fan2', 'GPU Fan 2'],
                    'GPU_USAGE':    ['GPU Core Load', 'GPU Usage', 'GPU Load'],
                    'VRAM_USAGE':   ['GPU Memory Usage', 'GPU Memory Load'],
                    'RAM_USAGE':    ['Physical Memory Load'],
                    'RAM_USED_GB':  ['Physical Memory Used'],
                    'NET_DOWN':     ['Current DL rate', 'Download rate'],
                    'NET_UP':       ['Current UP rate', 'Upload rate'],
                }
                auto_map = {}
                for key, candidates in KEY_NAMES.items():
                    for cname in candidates:
                        if cname in name_to_idx:
                            auto_map[key] = name_to_idx[cname]
                            break
                if auto_map:
                    cfg['sensor_map'].update(auto_map)
                    print(f'Sensor map auto-updated from discovered sensors ({len(auto_map)} keys)')
            except Exception as e:
                print(f'Sensor map auto-update error: {e}')
        elif 'sensorMap' in theme:
            # Fall back to theme's embedded map only if no discovered sensors file
            cfg['sensor_map'].update(theme['sensorMap'])
            print('Sensor map loaded from theme (no discovered sensors file found)')

        # Extract and register any custom fonts embedded in the theme
        load_custom_fonts_from_theme(theme)

        _current_theme = theme
        _current_theme_path = path
        cfg['theme_path'] = path
        save_cfg(cfg)
        print(f'Theme loaded: {theme.get("name","?")}')
        return True
    except Exception as e:
        print(f'Theme load error: {e}')
        return False

# ── Serial / streaming ────────────────────────────────────────────────────────
_port       = None
_running    = False
_run_thread = None

def open_port():
    global _port
    try:
        if _port and _port.is_open: _port.close()
        _port = serial.Serial(cfg['com_port'], baudrate=115200, timeout=2)
        return True
    except Exception as e:
        print(f'Port error: {e}')
        return False

def stream_loop():
    global _running
    frame_count = 0
    interval = 1.0 / max(1, cfg.get('fps',6))
    while _running:
        t0 = time.time()
        try:
            if _current_theme is None:
                time.sleep(0.5); continue
            sensors = read_sensors()
            img     = render_frame(_current_theme, sensors)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=88, subsampling=0)
            jpeg = buf.getvalue()
            frame = make_frame(jpeg, first=(frame_count==0))
            if _port and _port.is_open:
                _port.write(frame)
                _port.flush()
            frame_count += 1
        except Exception as e:
            print(f'Stream error: {e}')
            time.sleep(1)
        elapsed = time.time()-t0
        if elapsed < interval:
            time.sleep(interval-elapsed)

def start_display():
    global _running, _run_thread
    if _running: return
    if not open_port(): return
    _running = True
    _run_thread = threading.Thread(target=stream_loop, daemon=True)
    _run_thread.start()
    update_tray_icon()
    print('Display started')

def stop_display():
    global _running
    _running = False
    time.sleep(0.3)
    if _port and _port.is_open: _port.close()
    update_tray_icon()
    print('Display stopped')

# ── Settings Window ───────────────────────────────────────────────────────────
_settings_win = None

def open_settings():
    global _settings_win
    # If already open, just bring it to front
    if _settings_win and _settings_win.winfo_exists():
        _settings_win.lift()
        _settings_win.focus_force()
        return

    # Use Toplevel (child of hidden root) — NOT tk.Tk() which breaks on reopen
    win = tk.Toplevel(_tk_root)
    win.title('DS916 Settings')
    win.geometry('570x680')
    win.configure(bg='#18181c')
    win.resizable(True, True)
    win.minsize(500, 500)
    win.lift()
    win.focus_force()
    _settings_win = win

    style = ttk.Style(win)
    style.theme_use('clam')
    style.configure('TLabel',     background='#18181c', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TEntry',     fieldbackground='#1f1f25', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TButton',    background='#1f1f25', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TCombobox',  fieldbackground='#1f1f25', foreground='#e8e6df')
    style.configure('TFrame',     background='#18181c')
    style.configure('TSpinbox',   fieldbackground='#1f1f25', foreground='#e8e6df')
    style.configure('TCheckbutton', background='#18181c', foreground='#e8e6df')
    style.configure('TNotebook',  background='#18181c')
    style.configure('TNotebook.Tab', background='#1f1f25', foreground='#888', padding=[8,4])
    style.map('TNotebook.Tab',
              background=[('selected','#252530')],
              foreground=[('selected','#00b4ff')])

    nb = ttk.Notebook(win)
    nb.pack(fill='both', expand=True, padx=10, pady=10)

    # ── Tab 1: General ────────────────────────────────────────────────────────
    t1 = ttk.Frame(nb); nb.add(t1, text='  General  ')

    def lbl(parent, text, row, col=0):
        ttk.Label(parent, text=text).grid(row=row, column=col, sticky='w', padx=8, pady=4)

    lbl(t1, 'COM Port:', 0)
    com_var = tk.StringVar(value=cfg['com_port'])
    ports = [p.device for p in serial.tools.list_ports.comports()]
    ttk.Combobox(t1, textvariable=com_var, values=ports, width=14, state='readonly').grid(
        row=0, column=1, sticky='w', padx=8, pady=4)

    lbl(t1, 'FPS:', 1)
    fps_var = tk.IntVar(value=cfg.get('fps', 6))
    ttk.Spinbox(t1, from_=1, to=30, textvariable=fps_var, width=6).grid(
        row=1, column=1, sticky='w', padx=8, pady=4)

    lbl(t1, 'HWiNFO Source:', 2)
    # Read actual current source from cfg, default to sharedmem
    current_src = cfg.get('hwinfo_source', 'sharedmem')
    src_options = ['sharedmem', 'registry']
    src_var = tk.StringVar(value=current_src)
    src_cb = ttk.Combobox(t1, textvariable=src_var, values=src_options,
                          width=14, state='readonly')
    src_cb.grid(row=2, column=1, sticky='w', padx=8, pady=4)
    # Force selection to current value
    if current_src in src_options:
        src_cb.current(src_options.index(current_src))
    ttk.Label(t1, text='sharedmem = richer data  |  registry = no time limit',
              font=('Segoe UI', 8), foreground='#555').grid(
        row=3, column=0, columnspan=3, sticky='w', padx=8)

    lbl(t1, 'Theme File:', 4)
    theme_var = tk.StringVar(value=cfg.get('theme_path', ''))
    ttk.Entry(t1, textvariable=theme_var, width=28).grid(
        row=4, column=1, sticky='ew', padx=8, pady=4)
    def browse_theme():
        p = filedialog.askopenfilename(
            parent=win, filetypes=[('DS916 Theme', '*.ds916theme *.zip')])
        if p: theme_var.set(p)
    ttk.Button(t1, text='Browse…', command=browse_theme).grid(row=4, column=2, padx=4, pady=4)

    auto_var = tk.BooleanVar(value=cfg.get('autostart', True))
    ttk.Checkbutton(t1, text='Start display automatically with Windows',
                    variable=auto_var).grid(row=5, column=0, columnspan=3, sticky='w', padx=8, pady=6)

    # ── Tab 2: HWiNFO ────────────────────────────────────────────────────────
    t3 = ttk.Frame(nb); nb.add(t3, text='  HWiNFO  ')

    ttk.Label(t3, text='HWiNFO64 Shared Memory — 12-Hour Limit Workaround',
              font=('Segoe UI', 10, 'bold'), foreground='#00b4ff').pack(
        anchor='w', padx=10, pady=(12,4))

    msg = (
        'HWiNFO64 free edition disables shared memory after 12 hours.\n'
        'A Windows Scheduled Task can automatically restart HWiNFO64\n'
        'every 11.5 hours to keep shared memory active indefinitely.\n\n'
        'The task runs silently in the background — no window appears.'
    )
    ttk.Label(t3, text=msg, font=('Segoe UI', 9), foreground='#aaa',
              justify='left', wraplength=460).pack(anchor='w', padx=10, pady=4)

    # Detect HWiNFO64 exe path
    hwinfo_path_var = tk.StringVar(value=cfg.get('hwinfo_path', ''))
    def detect_hwinfo():
        import glob
        candidates = [
            r'C:\Program Files\HWiNFO64\HWiNFO64.exe',
            r'C:\Program Files (x86)\HWiNFO64\HWiNFO64.exe',
        ] + glob.glob(r'C:\Users\*\AppData\Local\HWiNFO64\HWiNFO64.exe')
        for p in candidates:
            if os.path.exists(p):
                hwinfo_path_var.set(p)
                return
        messagebox.showinfo('DS916', 'HWiNFO64 not found in common locations.\nBrowse to locate it manually.', parent=win)

    def browse_hwinfo():
        p = filedialog.askopenfilename(
            parent=win, title='Locate HWiNFO64.exe',
            filetypes=[('HWiNFO64', 'HWiNFO64.exe'), ('Executable', '*.exe')])
        if p: hwinfo_path_var.set(p)

    hw_frame = ttk.Frame(t3); hw_frame.pack(fill='x', padx=10, pady=4)
    ttk.Label(hw_frame, text='HWiNFO64.exe:').grid(row=0, column=0, sticky='w', pady=2)
    ttk.Entry(hw_frame, textvariable=hwinfo_path_var, width=36).grid(row=0, column=1, padx=6, pady=2)
    ttk.Button(hw_frame, text='Detect', command=detect_hwinfo).grid(row=0, column=2, padx=2)
    ttk.Button(hw_frame, text='Browse…', command=browse_hwinfo).grid(row=0, column=3, padx=2)

    def task_exists():
        import subprocess
        r = subprocess.run(['schtasks', '/query', '/tn', 'DS916_HWiNFO_Restart'],
                          capture_output=True)
        return r.returncode == 0

    def install_task():
        path = hwinfo_path_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror('DS916', 'Please set the HWiNFO64.exe path first.', parent=win)
            return
        import subprocess
        # Task: run every 690 minutes (11.5 hours), starting 11.5h from now
        xml = f'''<?xml version="1.0"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <RepetitionTrigger>
      <Repetition>
        <Interval>PT690M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </RepetitionTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-WindowStyle Hidden -Command "Stop-Process -Name HWiNFO64 -Force -ErrorAction SilentlyContinue; Start-Sleep 3; Start-Process '{path}' -ArgumentList '-sensors'"</Arguments>
    </Exec>
  </Actions>
  <Settings>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
  </Settings>
</Task>'''
        xml_path = os.path.join(os.environ.get('TEMP','C:\\Temp'), 'ds916_hwinfo_task.xml')
        with open(xml_path, 'w') as f: f.write(xml)
        r = subprocess.run(
            ['schtasks', '/create', '/tn', 'DS916_HWiNFO_Restart',
             '/xml', xml_path, '/f'],
            capture_output=True, text=True)
        os.unlink(xml_path)
        if r.returncode == 0:
            cfg['hwinfo_path'] = path
            save_cfg(cfg)
            messagebox.showinfo('DS916',
                'Scheduled task created!\n\nHWiNFO64 will restart every 11.5 hours\nto keep shared memory active.', parent=win)
            update_task_btn()
        else:
            messagebox.showerror('DS916', f'Failed to create task:\n{r.stderr}', parent=win)

    def remove_task():
        import subprocess
        r = subprocess.run(
            ['schtasks', '/delete', '/tn', 'DS916_HWiNFO_Restart', '/f'],
            capture_output=True, text=True)
        if r.returncode == 0:
            messagebox.showinfo('DS916', 'Scheduled task removed.', parent=win)
        update_task_btn()

    def update_task_btn():
        exists = task_exists()
        install_btn.config(state='disabled' if exists else 'normal')
        remove_btn.config(state='normal' if exists else 'disabled')
        status_lbl.config(
            text='✅ Task installed — HWiNFO64 will restart every 11.5 hours' if exists
                 else '○ Task not installed',
            foreground='#4fc87a' if exists else '#888')

    btn_row = ttk.Frame(t3); btn_row.pack(anchor='w', padx=10, pady=8)
    install_btn = ttk.Button(btn_row, text='Install Restart Task', command=install_task)
    install_btn.pack(side='left', padx=(0,6))
    remove_btn = ttk.Button(btn_row, text='Remove Task', command=remove_task)
    remove_btn.pack(side='left')

    status_lbl = ttk.Label(t3, text='', font=('Segoe UI', 9))
    status_lbl.pack(anchor='w', padx=10)
    update_task_btn()

    # ── Tab 3: Sensor Map ─────────────────────────────────────────────────────
    t2 = ttk.Frame(nb); nb.add(t2, text='  Sensor Map  ')
    ttk.Label(t2, text='Map each sensor key to its HWiNFO64 registry index (ValueRawN).\nLeave blank if not available.',
              font=('Segoe UI', 9), foreground='#888').pack(anchor='w', padx=10, pady=(8,4))

    frame2 = ttk.Frame(t2); frame2.pack(fill='both', expand=True, padx=10, pady=4)
    sm = cfg['sensor_map']
    SENSOR_LABELS = {
        'CPU_USAGE':'CPU Usage %', 'CPU_TEMP':'CPU Temp °C', 'MB_TEMP':'Motherboard Temp',
        'CPU_FAN':'CPU Fan RPM', 'CHASSIS_FAN1':'Chassis Fan 1', 'CHASSIS_FAN2':'Chassis Fan 2',
        'GPU_TEMP':'GPU Temp °C', 'GPU_FAN1':'GPU Fan 1', 'GPU_FAN2':'GPU Fan 2',
        'GPU_USAGE':'GPU Usage %', 'RAM_USAGE':'RAM Usage %', 'RAM_USED_GB':'RAM Used GB',
        'NET_DOWN':'Net Download', 'NET_UP':'Net Upload'
    }
    sm_vars = {}
    for row, (key, lname) in enumerate(SENSOR_LABELS.items()):
        ttk.Label(frame2, text=lname, width=20).grid(row=row, column=0, sticky='w', pady=2)
        ttk.Label(frame2, text=key, width=16, foreground='#555').grid(row=row, column=1, sticky='w', pady=2)
        v = tk.StringVar(value='' if sm.get(key) is None else str(sm[key]))
        ttk.Entry(frame2, textvariable=v, width=8).grid(row=row, column=2, padx=6, pady=2)
        sm_vars[key] = v

    # ── Save / Cancel ─────────────────────────────────────────────────────────
    def save_settings():
        cfg['com_port']      = com_var.get()
        cfg['fps']           = fps_var.get()
        cfg['hwinfo_source'] = src_var.get()
        cfg['theme_path']    = theme_var.get()
        cfg['autostart']     = auto_var.get()
        for key, v in sm_vars.items():
            s = v.get().strip()
            cfg['sensor_map'][key] = None if s == '' else int(s)
        save_cfg(cfg)
        set_autostart(cfg['autostart'])
        if cfg['theme_path'] and cfg['theme_path'] != _current_theme_path:
            load_theme(cfg['theme_path'])
        win.destroy()

    btn_f = ttk.Frame(win); btn_f.pack(fill='x', padx=10, pady=8)
    ttk.Button(btn_f, text='Cancel', command=win.destroy).pack(side='right', padx=4)
    ttk.Button(btn_f, text='Save', command=save_settings).pack(side='right', padx=4)

# ── Windows autostart ─────────────────────────────────────────────────────────
def set_autostart(enable):
    key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
    exe = sys.executable if getattr(sys,'frozen',False) else os.path.abspath(__file__)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except: pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f'Autostart error: {e}')

def is_autostart():
    key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except: return False

# ── Tray icon ─────────────────────────────────────────────────────────────────
_tray = None

def make_tray_icon():
    """Create a simple DS916 icon programmatically."""
    size = 64
    img = PILImage.new('RGBA', (size,size), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.ellipse([4,4,60,60], fill=(0,20,30,255), outline=(0,180,255,255), width=3)
    d.rectangle([20,16,44,48], fill=(0,180,255,200))
    d.rectangle([22,18,42,46], fill=(0,10,20,255))
    for i in range(3):
        y0=22+i*8; d.rectangle([25,y0,39,y0+5],fill=(0,180,255,180))
    return img

def update_tray_icon():
    global _tray
    if not _tray: return
    status = '● Running' if _running else '○ Stopped'
    theme_name = _current_theme.get('name','No theme') if _current_theme else 'No theme loaded'
    _tray.title = f'DS916 — {status}\n{theme_name}'

# ── Main-thread dispatcher ────────────────────────────────────────────────────
# pystray callbacks run on a background thread. tkinter dialogs MUST run on the
# main thread. We use a queue + tk.after() poll to bridge the two safely.
import queue
_ui_queue = queue.Queue()
_tk_root  = None   # set in main before run_tray()

def _dispatch(fn, *args, **kwargs):
    """Schedule fn(*args,**kwargs) to run on the main tk thread."""
    _ui_queue.put((fn, args, kwargs))

def _poll_ui_queue():
    """Called every 100ms on the main tk thread to drain the queue."""
    try:
        while True:
            fn, args, kwargs = _ui_queue.get_nowait()
            fn(*args, **kwargs)
    except queue.Empty:
        pass
    finally:
        if _tk_root:
            _tk_root.after(100, _poll_ui_queue)

# ── Tray callbacks (run on pystray thread → dispatched to main thread) ────────
def load_theme_dialog(icon=None, item=None):
    _dispatch(_load_theme_dialog_main)

def _load_theme_dialog_main():
    from tkinter import filedialog
    path = filedialog.askopenfilename(
        parent=_tk_root,
        title='Load DS916 Theme',
        filetypes=[('DS916 Theme', '*.zip *.ds916theme'), ('All files', '*.*')]
    )
    if path:
        if load_theme(path):
            if not _running: start_display()
            update_tray_icon()
            print(f'Theme loaded and display started: {path}')

def toggle_display(icon=None, item=None):
    if _running: stop_display()
    else: start_display()
    update_tray_icon()

def open_builder(icon=None, item=None):
    import webbrowser
    exe_dir = os.path.dirname(sys.executable if getattr(sys,'frozen',False) else os.path.abspath(__file__))
    html = os.path.join(exe_dir, 'theme_builder.html')
    if os.path.exists(html):
        webbrowser.open('file:///'+html.replace('\\','/'))
    else:
        _dispatch(lambda: messagebox.showwarning('DS916',
            'theme_builder.html not found next to this exe.\n\nPlace it in:\n'+exe_dir))

def open_settings_tray(icon=None, item=None):
    _dispatch(open_settings)

def quit_app(icon=None, item=None):
    stop_display()
    if _tray: _tray.stop()
    if _tk_root:
        try: _tk_root.quit()
        except: pass

def uninstall_app(icon=None, item=None):
    _dispatch(_uninstall_main)

def _uninstall_main():
    result = messagebox.askyesno(
        'DS916 Tray — Uninstall',
        'This will:\n\n'
        '  • Remove DS916Tray from Windows startup\n'
        '  • Delete config and data from AppData\n'
        '  • Close the tray app\n\n'
        'Your theme files will NOT be deleted.\n\n'
        'Continue?',
        parent=_tk_root
    )
    if not result:
        return

    # 1. Remove from startup
    set_autostart(False)

    # 2. Delete config folder
    import shutil
    if os.path.exists(CONFIG_DIR):
        try:
            shutil.rmtree(CONFIG_DIR)
            print(f'Removed config: {CONFIG_DIR}')
        except Exception as e:
            print(f'Config removal error: {e}')

    # 3. Clean up temp font files
    for path in _custom_font_files.values():
        try:
            if os.path.exists(path):
                os.unlink(path)
        except: pass

    messagebox.showinfo(
        'DS916 Tray — Uninstalled',
        'DS916 Tray has been removed from startup and AppData.\n\n'
        'You can now delete DS916Tray.exe and theme_builder.html manually.',
        parent=_tk_root
    )

    # 4. Exit
    quit_app()

_status_win = None

def show_status(icon=None, item=None):
    _dispatch(_show_status_main)

def _show_status_main():
    global _status_win
    if _status_win and _status_win.winfo_exists():
        _status_win.lift(); _status_win.focus_force(); return

    win = tk.Toplevel(_tk_root)
    win.title('DS916 Status')
    win.geometry('440x480')
    win.configure(bg='#0f0f11')
    win.resizable(True, True)
    win.minsize(380, 360)
    win.lift(); win.focus_force()
    _status_win = win

    BG   = '#0f0f11'
    PAN  = '#18181c'
    ACC  = '#00b4ff'
    GRN  = '#4fc87a'
    RED  = '#e05a4b'
    MUT  = '#666'
    TXT  = '#e8e6df'

    # Header (fixed, outside scroll)
    hdr = tk.Frame(win, bg=ACC, padx=12, pady=8)
    hdr.pack(fill='x')
    tk.Label(hdr, text='⬡ DS916 Screen Manager', bg=ACC, fg='#000',
             font=('Segoe UI', 11, 'bold')).pack(side='left')
    status_text = '● Running' if _running else '○ Stopped'
    status_col  = '#003020' if _running else '#300000'
    tk.Label(hdr, text=status_text, bg=status_col, fg=GRN if _running else RED,
             font=('Segoe UI', 9, 'bold'), padx=8, pady=2).pack(side='right')

    # Scrollable body
    body_frame = tk.Frame(win, bg=BG)
    body_frame.pack(fill='both', expand=True)

    canvas = tk.Canvas(body_frame, bg=BG, highlightthickness=0)
    scrollbar = tk.Scrollbar(body_frame, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)

    inner = tk.Frame(canvas, bg=BG)
    inner_id = canvas.create_window((0,0), window=inner, anchor='nw')

    def on_configure(e):
        canvas.configure(scrollregion=canvas.bbox('all'))
    def on_canvas_resize(e):
        canvas.itemconfig(inner_id, width=e.width)
    inner.bind('<Configure>', on_configure)
    canvas.bind('<Configure>', on_canvas_resize)
    canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), 'units'))

    def section(text):
        f = tk.Frame(inner, bg=PAN, padx=10, pady=6)
        f.pack(fill='x', padx=12, pady=(6,0))
        tk.Label(f, text=text, bg=PAN, fg=ACC,
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        return f

    def row(parent, label, value, value_color=TXT):
        f = tk.Frame(parent, bg=PAN)
        f.pack(fill='x', pady=1)
        tk.Label(f, text=label, bg=PAN, fg=MUT,
                 font=('Segoe UI', 9), width=18, anchor='w').pack(side='left')
        tk.Label(f, text=value, bg=PAN, fg=value_color,
                 font=('Segoe UI', 9, 'bold'), anchor='w', wraplength=260,
                 justify='left').pack(side='left', fill='x', expand=True)

    # Display
    s1 = section('Display')
    row(s1, 'Status',       '● Streaming to screen' if _running else '○ Stopped',
        GRN if _running else RED)
    row(s1, 'COM Port',     cfg.get('com_port', 'COM3'))
    row(s1, 'Target FPS',   str(cfg.get('fps', 6)))
    theme_name = _current_theme.get('name', '—') if _current_theme else '—'
    theme_res  = (f"{_current_theme.get('width',462)}×{_current_theme.get('height',1920)}"
                  if _current_theme else '—')
    row(s1, 'Theme',        theme_name)
    row(s1, 'Resolution',   theme_res)

    # HWiNFO
    s2 = section('HWiNFO64 Sensor Source')
    src = cfg.get('hwinfo_source', 'sharedmem')
    shm_active = src == 'sharedmem' and _shm_handle is not None
    if src == 'sharedmem':
        if shm_active:
            src_label = 'Shared Memory  ✅'
            src_col   = GRN
        else:
            src_label = 'Shared Memory (unavailable — using Registry fallback)'
            src_col   = '#d4b84a'
    else:
        src_label = 'Registry (VSB Gadget)'
        src_col   = TXT
    row(s2, 'Source',       src_label, src_col)

    import subprocess
    task_ok = subprocess.run(
        ['schtasks', '/query', '/tn', 'DS916_HWiNFO_Restart'],
        capture_output=True).returncode == 0
    row(s2, 'Auto-restart',
        '✅ Task installed (every 11.5h)' if task_ok else '○ Not configured',
        GRN if task_ok else MUT)

    # Sensors
    s3 = section('Live Sensor Snapshot')
    try:
        sensors = read_sensors()
        pairs = [
            ('CPU Usage',    sensors.get('CPU_USAGE'),  '%'),
            ('CPU Temp',     sensors.get('CPU_TEMP'),   '°C'),
            ('GPU Usage',    sensors.get('GPU_USAGE'),  '%'),
            ('GPU Temp',     sensors.get('GPU_TEMP'),   '°C'),
            ('MB Temp',      sensors.get('MB_TEMP'),    '°C'),
            ('CPU Fan',      sensors.get('CPU_FAN'),    'RPM'),
        ]
        for lbl, val, unit in pairs:
            if val is not None:
                row(s3, lbl, f'{val:.1f} {unit}')
            else:
                row(s3, lbl, '— (not mapped)', MUT)
    except Exception as e:
        row(s3, 'Error', str(e), RED)

    # System
    s4 = section('System')
    row(s4, 'Windows Startup', '✅ Enabled' if is_autostart() else '○ Disabled',
        GRN if is_autostart() else MUT)

    # Buttons (inside inner so they scroll with content)
    btn_f = tk.Frame(inner, bg=BG)
    btn_f.pack(fill='x', padx=12, pady=10)
    tk.Button(btn_f, text='↻ Refresh', bg=PAN, fg=ACC,
              font=('Segoe UI', 9), bd=0, padx=12, pady=4,
              command=lambda: [win.destroy(), _show_status_main()]).pack(side='left')
    tk.Button(btn_f, text='Close', bg=PAN, fg=TXT,
              font=('Segoe UI', 9), bd=0, padx=12, pady=4,
              command=win.destroy).pack(side='right')

def discover_sensors_tray(icon=None, item=None):
    _dispatch(_discover_sensors_main)

def _discover_sensors_main():
    path = discover_sensors()
    if path:
        messagebox.showinfo('DS916 — Sensor Discovery',
            f'✅ {len(json.load(open(path))["sensors"])} sensors discovered and saved.\n\n'
            f'{path}\n\n'
            'Open the Theme Builder and click "Import Sensor List" to use them.',
            parent=_tk_root)
    else:
        messagebox.showwarning('DS916 — Sensor Discovery',
            'Could not discover sensors.\n'
            'Make sure HWiNFO64 is running with Shared Memory enabled.',
            parent=_tk_root)

def build_menu():
    disp_label = '⏹ Stop Display' if _running else '▶ Start Display'
    return pystray.Menu(
        Item('DS916 Screen Manager', None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item(disp_label, toggle_display),
        Item('📂 Load Theme…', load_theme_dialog),
        Item('🎨 Open Theme Builder', open_builder),
        pystray.Menu.SEPARATOR,
        Item('🔍 Discover Sensors', discover_sensors_tray),
        Item('ℹ Status…', show_status),
        Item('⚙ Settings…', open_settings_tray),
        pystray.Menu.SEPARATOR,
        Item('🗑 Uninstall…', uninstall_app),
        Item('❌ Exit', quit_app),
    )

def run_tray():
    global _tray
    icon_img = make_tray_icon()
    _tray = pystray.Icon(APP_NAME, icon_img, 'DS916 Screen Manager', menu=build_menu())
    # Run pystray on its own thread so the main thread stays free for tk
    t = threading.Thread(target=_tray.run, daemon=True)
    t.start()

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Create a hidden tk root on the MAIN thread — must happen before anything
    # that needs dialogs.  All file dialogs and message boxes use this root.
    _tk_root = tk.Tk()
    _tk_root.withdraw()          # keep it invisible
    _tk_root.after(100, _poll_ui_queue)   # start queue polling

    # Try shared memory if configured — don't permanently overwrite cfg on failure,
    # just let read_sensors fall back to registry each call
    if cfg.get('hwinfo_source', 'sharedmem') == 'sharedmem':
        if try_open_sharedmem():
            print('HWiNFO source: Shared Memory ✅')
            # Auto-discover and save sensors on every startup
            discover_sensors()
        else:
            print('HWiNFO source: Shared Memory failed — falling back to Registry for now')
            print('  (will retry each sensor read; start HWiNFO64 with Shared Memory enabled)')
    else:
        print('HWiNFO source: Registry (VSB Gadget)')

    # Set autostart if configured
    if cfg.get('autostart', True):
        set_autostart(True)

    # Auto-load last theme
    if cfg.get('theme_path') and os.path.exists(cfg['theme_path']):
        if load_theme(cfg['theme_path']):
            start_display()

    # Start tray icon on background thread
    run_tray()

    # Main thread runs tk event loop (handles dialogs safely)
    try:
        _tk_root.mainloop()
    except KeyboardInterrupt:
        quit_app()
