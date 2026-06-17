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
    'hwinfo_source': 'registry',   # 'registry' or 'sharedmem'
    'sensor_map': {
        'CPU_USAGE':0,'CPU_TEMP':1,'MB_TEMP':2,'CPU_FAN':3,
        'CHASSIS_FAN1':4,'CHASSIS_FAN2':5,'GPU_TEMP':6,
        'GPU_FAN1':7,'GPU_FAN2':8,'GPU_USAGE':9,
        'RAM_USAGE':None,'RAM_USED_GB':None,'NET_DOWN':None,'NET_UP':None
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
    """Try to open HWiNFO shared memory. Returns True on success."""
    global _shm_handle, _shm_data
    try:
        import mmap
        SHM_NAME = 'Global\\HWiNFO_SENS_SM2'
        _shm_handle = mmap.mmap(-1, 0, SHM_NAME, access=mmap.ACCESS_READ)
        _shm_data = _shm_handle
        return True
    except Exception as e:
        print(f'Shared memory unavailable: {e}')
        return False

def read_sharedmem(sensor_map):
    """Read from HWiNFO shared memory. Returns {KEY: value} dict."""
    # Shared memory layout (simplified):
    # Header (40 bytes): version, num_sensors, num_readings, ...
    # Sensor entries follow
    # This is a known reverse-engineered format
    data = {}
    try:
        _shm_data.seek(0)
        raw = _shm_data.read(40)
        sig       = struct.unpack_from('<I', raw, 0)[0]
        if sig != 0x57494648:  # 'WIFH' signature
            return data
        n_sensors = struct.unpack_from('<I', raw, 4)[0]
        n_entries = struct.unpack_from('<I', raw, 8)[0]
        off_s     = struct.unpack_from('<I', raw, 12)[0]
        sz_s      = struct.unpack_from('<I', raw, 16)[0]
        off_e     = struct.unpack_from('<I', raw, 20)[0]
        sz_e      = struct.unpack_from('<I', raw, 24)[0]

        # Read entries (readings)
        for i in range(n_entries):
            _shm_data.seek(off_e + i*sz_e)
            entry = _shm_data.read(sz_e)
            if len(entry) < 68: continue
            # Value is a double at offset 52
            val   = struct.unpack_from('<d', entry, 52)[0]
            idx   = i
            for key, midx in sensor_map.items():
                if midx is not None and midx == idx:
                    data[key] = val
    except Exception as e:
        print(f'Shared memory read error: {e}')
    return data

def read_registry(sensor_map):
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
    sm = cfg.get('sensor_map', DEFAULT_CFG['sensor_map'])
    src = cfg.get('hwinfo_source','registry')
    if src == 'sharedmem' and _shm_data:
        d = read_sharedmem(sm)
        if d: return d
    # Fall back to registry
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
                raw = sv(el.get('sensorKey',''), 0)
                if isinstance(raw, float) and raw == int(raw):
                    val_str = str(int(raw))
                elif isinstance(raw, float):
                    val_str = f'{raw:.1f}'
                else:
                    val_str = str(raw)
                text = pre + val_str + unit

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

        # Update sensor map from theme if present
        if 'sensorMap' in theme:
            cfg['sensor_map'].update(theme['sensorMap'])

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
    if _settings_win and _settings_win.winfo_exists():
        _settings_win.lift(); return

    win = tk.Tk()
    win.title('DS916 Settings')
    win.geometry('520x640')
    win.configure(bg='#18181c')
    win.resizable(False, False)
    _settings_win = win

    style = ttk.Style(win)
    style.theme_use('clam')
    style.configure('TLabel',  background='#18181c', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TEntry',  fieldbackground='#1f1f25', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TButton', background='#1f1f25', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TCombobox', fieldbackground='#1f1f25', foreground='#e8e6df')
    style.configure('TFrame',  background='#18181c')
    style.configure('TLabelframe', background='#18181c', foreground='#00b4ff')
    style.configure('TLabelframe.Label', background='#18181c', foreground='#00b4ff', font=('Segoe UI',10,'bold'))
    style.configure('TNotebook', background='#18181c')
    style.configure('TNotebook.Tab', background='#1f1f25', foreground='#888', padding=[8,4])
    style.map('TNotebook.Tab', background=[('selected','#252530')], foreground=[('selected','#00b4ff')])

    nb = ttk.Notebook(win); nb.pack(fill='both', expand=True, padx=10, pady=10)

    # ── Tab 1: General ────────────────────────────────────────────────────────
    t1 = ttk.Frame(nb); nb.add(t1, text='  General  ')

    def lbl(parent, text, row, col=0, **kw):
        ttk.Label(parent, text=text).grid(row=row, column=col, sticky='w', padx=8, pady=4, **kw)

    lbl(t1,'COM Port:',0); com_var=tk.StringVar(value=cfg['com_port'])
    ports=[p.device for p in serial.tools.list_ports.comports()]
    cb=ttk.Combobox(t1,textvariable=com_var,values=ports,width=14,state='readonly')
    cb.grid(row=0,column=1,sticky='w',padx=8,pady=4)

    lbl(t1,'FPS:',1); fps_var=tk.IntVar(value=cfg.get('fps',6))
    ttk.Spinbox(t1,from_=1,to=30,textvariable=fps_var,width=6).grid(row=1,column=1,sticky='w',padx=8,pady=4)

    lbl(t1,'HWiNFO Source:',2); src_var=tk.StringVar(value=cfg.get('hwinfo_source','registry'))
    ttk.Combobox(t1,textvariable=src_var,values=['registry','sharedmem'],width=14,state='readonly').grid(row=2,column=1,sticky='w',padx=8,pady=4)
    ttk.Label(t1,text='(registry=no limit; sharedmem=richer data, 12h free limit)',
              font=('Segoe UI',8),foreground='#666').grid(row=3,column=0,columnspan=3,sticky='w',padx=8)

    lbl(t1,'Theme File:',4); theme_var=tk.StringVar(value=cfg.get('theme_path',''))
    ttk.Entry(t1,textvariable=theme_var,width=30).grid(row=4,column=1,sticky='ew',padx=8,pady=4)
    def browse_theme():
        p=filedialog.askopenfilename(filetypes=[('DS916 Theme','*.zip *.ds916theme')])
        if p: theme_var.set(p)
    ttk.Button(t1,text='Browse…',command=browse_theme).grid(row=4,column=2,padx=4,pady=4)

    auto_var=tk.BooleanVar(value=cfg.get('autostart',True))
    ttk.Checkbutton(t1,text='Start display automatically with Windows',variable=auto_var).grid(row=5,column=0,columnspan=3,sticky='w',padx=8,pady=6)

    # ── Tab 2: Sensor Map ─────────────────────────────────────────────────────
    t2 = ttk.Frame(nb); nb.add(t2, text='  Sensor Map  ')
    ttk.Label(t2,text='Map each sensor key to its HWiNFO64 registry index (ValueRawN).\nLeave blank if not available.',
              font=('Segoe UI',9),foreground='#888').pack(anchor='w',padx=10,pady=(8,4))

    frame2=ttk.Frame(t2); frame2.pack(fill='both',expand=True,padx=10,pady=4)
    sm=cfg['sensor_map']
    SENSOR_LABELS={
        'CPU_USAGE':'CPU Usage %','CPU_TEMP':'CPU Temp °C','MB_TEMP':'Motherboard Temp',
        'CPU_FAN':'CPU Fan RPM','CHASSIS_FAN1':'Chassis Fan 1','CHASSIS_FAN2':'Chassis Fan 2',
        'GPU_TEMP':'GPU Temp °C','GPU_FAN1':'GPU Fan 1','GPU_FAN2':'GPU Fan 2',
        'GPU_USAGE':'GPU Usage %','RAM_USAGE':'RAM Usage %','RAM_USED_GB':'RAM Used GB',
        'NET_DOWN':'Net Download','NET_UP':'Net Upload'
    }
    sm_vars={}
    for row,(key,lname) in enumerate(SENSOR_LABELS.items()):
        ttk.Label(frame2,text=lname,width=20).grid(row=row,column=0,sticky='w',pady=2)
        ttk.Label(frame2,text=key,width=16,foreground='#555').grid(row=row,column=1,sticky='w',pady=2)
        v=tk.StringVar(value='' if sm.get(key) is None else str(sm[key]))
        ttk.Entry(frame2,textvariable=v,width=8).grid(row=row,column=2,padx=6,pady=2)
        sm_vars[key]=v

    # ── Save ──────────────────────────────────────────────────────────────────
    def save_settings():
        cfg['com_port']       = com_var.get()
        cfg['fps']            = fps_var.get()
        cfg['hwinfo_source']  = src_var.get()
        cfg['theme_path']     = theme_var.get()
        cfg['autostart']      = auto_var.get()
        for key,v in sm_vars.items():
            s=v.get().strip()
            cfg['sensor_map'][key] = None if s=='' else int(s)
        save_cfg(cfg)
        set_autostart(cfg['autostart'])
        # Reload theme if path changed
        if cfg['theme_path'] and cfg['theme_path']!=_current_theme_path:
            load_theme(cfg['theme_path'])
        win.destroy()

    btn_f=ttk.Frame(win); btn_f.pack(fill='x',padx=10,pady=8)
    ttk.Button(btn_f,text='Cancel',command=win.destroy).pack(side='right',padx=4)
    ttk.Button(btn_f,text='Save',command=save_settings).pack(side='right',padx=4)
    win.mainloop()

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

def build_menu():
    disp_label = '⏹ Stop Display' if _running else '▶ Start Display'
    return pystray.Menu(
        Item('DS916 Screen Manager', None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item(disp_label, toggle_display),
        Item('📂 Load Theme…', load_theme_dialog),
        Item('🎨 Open Theme Builder', open_builder),
        pystray.Menu.SEPARATOR,
        Item('⚙ Settings…', open_settings_tray),
        pystray.Menu.SEPARATOR,
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

    # Try shared memory if configured
    if cfg.get('hwinfo_source') == 'sharedmem':
        if not try_open_sharedmem():
            print('Falling back to registry mode')
            cfg['hwinfo_source'] = 'registry'

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
