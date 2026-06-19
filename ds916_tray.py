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
THEMES_DIR  = os.path.join(CONFIG_DIR, 'Themes')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(THEMES_DIR, exist_ok=True)

# ── Default config ────────────────────────────────────────────────────────────
DEFAULT_CFG = {
    'com_port':   'COM3',
    'fps':        6,
    'theme_path': '',
    'autostart':  True,
    'hwinfo_path': '',
    'rtss_process': '',              # empty = auto-detect active 3D app; or an exact exe name (e.g. "game.exe") to pin a specific process
    # Note: there is no persisted sensor index map here. Standard sensor
    # keys (CPU_USAGE, GPU_TEMP, etc.) are resolved fresh by NAME on every
    # single read inside read_sharedmem() -- nothing is ever cached across
    # restarts, so there is nothing that can go stale if HWiNFO's internal
    # sensor ordering shifts (driver update, new device added, etc.).
    # CUSTOM_N entries for sensors with no standard name come from the
    # currently loaded theme's own sensorMap instead of a global config.
}

def load_cfg():
    try:
        with open(CONFIG_FILE) as f: c=json.load(f)
        # Merge with defaults for any missing keys
        for k,v in DEFAULT_CFG.items():
            if k not in c: c[k]=v
        # Drop any leftover sensor_map from an older config version -- it's
        # no longer used for anything and keeping it around risks confusion
        # (and was the source of a real bug: a stale cached index could
        # silently persist forever across HWiNFO restarts/reorderings).
        c.pop('sensor_map', None)
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


# Canonical HWiNFO sensor names for each standard sensor key. Looked up by
# NAME on every single read (like the RTSS reader already does for process
# names) rather than caching a numeric index anywhere — this is what
# actually eliminates the staleness problem: there's nothing to go stale
# if nothing is ever persisted across HWiNFO restarts/reorderings.
STANDARD_SENSOR_NAMES = {
    'CPU_USAGE':    ['Total CPU Usage'],
    'CPU_TEMP':     ['CPU (Tctl/Tdie)', 'CPU Package', 'CPU Temperature'],
    'CPU_FAN':      ['CPU1', 'CPU Fan', 'CPU_OPT'],
    'CPU_FREQ':     ['CPU Clock', 'Core Clocks (avg)'],
    'CPU_POWER':    ['CPU Package Power', 'CPU Power'],
    'CPU_VOLTAGE':  ['CPU Core Voltage', 'Vcore'],
    'GPU_USAGE':    ['GPU Core Load', 'GPU Usage', 'GPU Load'],
    'GPU_TEMP':     ['GPU Temperature', 'GPU Temp'],
    'GPU_FAN1':     ['GPU Fan1', 'GPU Fan 1'],
    'GPU_FAN2':     ['GPU Fan2', 'GPU Fan 2'],
    'GPU_FREQ':     ['GPU Clock'],
    'GPU_POWER':    ['GPU Power'],
    'VRAM_USAGE':   ['GPU Memory Usage', 'GPU Memory Load'],
    'VRAM_USED':    ['GPU Memory Used'],
    'RAM_USAGE':    ['Physical Memory Load'],
    'RAM_USED_GB':  ['Physical Memory Used'],
    'RAM_FREE_GB':  ['Physical Memory Available'],
    'RAM_TOTAL':    ['Physical Memory Total'],
    'DISK_USAGE':   ['Disk Usage'],
    'DISK_USED':    ['Disk Used'],
    'DISK_FREE':    ['Disk Free'],
    'DISK_TEMP':    ['Drive Temperature'],
    'DISK_READ':    ['Read Rate', 'Disk Read Rate'],
    'DISK_WRITE':   ['Write Rate', 'Disk Write Rate'],
    'MB_TEMP':      ['Motherboard'],
    'CHASSIS_FAN1': ['Chassis1', 'Chassis Fan 1', 'CHA_FAN1'],
    'CHASSIS_FAN2': ['Chassis2', 'Chassis Fan 2', 'CHA_FAN2'],
    'CHASSIS_FAN3': ['Chassis3', 'Chassis Fan 3', 'CHA_FAN3'],
    'NET_DOWN':     ['Current DL rate', 'Download rate'],
    'NET_UP':       ['Current UP rate', 'Upload rate'],
    'NET_PING':     ['Ping'],
    'BATTERY':      ['Battery Charge Level'],
    # FRAMERATE intentionally not auto-mapped — HWiNFO's PresentMon
    # tracking is unreliable without an HWiNFO Pro license; users who
    # want to try it anyway can wire up a CUSTOM_N key manually.
}

def read_sharedmem(sensor_map=None):
    """Read sensor values from HWiNFO shared memory, resolving every
    standard sensor key (and any CUSTOM_N keys) fresh by NAME on every
    call. No numeric index is ever cached in config.json — if HWiNFO's
    internal sensor ordering shifts after a restart, driver update, or
    new device being added, this re-resolves correctly on the very next
    read with no stale state possible.

    sensor_map is accepted for backwards compatibility (CUSTOM_N entries
    from older saved themes may still carry a literal index) but standard
    keys always resolve by name, ignoring any cached index for them.

    Layout from: github.com/namazso/hwinfosharedmem.h
    HWiNFOEntry: type(4) sensor_index(4) id(4) name_orig(128) name_user(128) unit(16) value(8d)
    Value is a double at offset 0x11C = 284 within each entry.
    """
    global _shm_handle
    data = {}
    if not _shm_handle: return data
    try:
        import ctypes
        kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e = _shm_handle
        ptr = int(ptr)
        if not ptr or sz_e < 285 or n_e <= 0: return data

        # Re-verify the signature on every read. If HWiNFO64 restarts (e.g.
        # after the free version's 12-hour shared memory limit kicks in and
        # the user restarts it), the OLD mapping we have open becomes stale
        # — Windows may have destroyed and recreated the section under the
        # same name. Without this check we'd keep silently failing forever
        # on a dead handle, since _shm_handle would never go back to None
        # and try_open_sharedmem() would never be called again.
        VALID_SIGS = {0x57494648, 0x53695748}
        sig_bytes = ctypes.string_at(ptr, 4)
        sig = struct.unpack('<I', sig_bytes)[0]
        if sig not in VALID_SIGS:
            print('HWiNFO shared memory signature is now invalid (stale handle after a restart) - reconnecting next read', flush=True)
            try:
                kernel32.UnmapViewOfFile(ptr)
                kernel32.CloseHandle(win_handle)
            except Exception:
                pass
            _shm_handle = None
            return data

        VALUE_OFFSET = 0x11C  # 284 — double at this offset within HWiNFOEntry

        # Build name -> (index, value) for every entry in ONE pass, then
        # resolve every standard key against it by name. This is the same
        # cost as before (one scan of all entries per read) but eliminates
        # any persisted index entirely.
        name_to_val = {}
        for idx in range(n_e):
            entry_ptr = ptr + off_e + idx * sz_e
            name_bytes = ctypes.string_at(entry_ptr + 0x0C, 128)
            name = name_bytes.rstrip(b'\x00').decode('ascii', 'replace').strip()
            if not name: continue
            val_bytes = ctypes.string_at(entry_ptr + VALUE_OFFSET, 8)
            val = struct.unpack('<d', val_bytes)[0]
            name_to_val[name] = val

        for key, candidates in STANDARD_SENSOR_NAMES.items():
            for cname in candidates:
                if cname in name_to_val:
                    data[key] = name_to_val[cname]
                    break

        # CUSTOM_N keys (manually wired sensors not covered by the standard
        # name table above) still use whatever literal index was saved with
        # the theme/sensor_map, since there's no name to re-resolve against
        # for an arbitrary user-picked index.
        if sensor_map:
            for skey, idx in sensor_map.items():
                if not skey.startswith('CUSTOM_') or idx is None: continue
                if idx >= n_e: continue
                entry_ptr = ptr + off_e + idx * sz_e
                val_bytes = ctypes.string_at(entry_ptr + VALUE_OFFSET, 8)
                data[skey] = struct.unpack('<d', val_bytes)[0]

    except Exception as e:
        print(f'Shared memory read error: {e}')
        # Any unexpected failure also resets the handle, rather than
        # leaving a possibly-broken mapping in place indefinitely.
        _shm_handle = None
    return data


def discover_sensors():
    """Scan all HWiNFO shared memory entries and save to hwinfo_sensors.json.
    Also reads the sensor group (device name) section so device names like
    'AMD Ryzen 5 5600X' and 'ASRock B550M Steel Legend' are available in
    the theme builder's + Sensors picker as static label elements.
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

        # Header layout (_HWiNFO_SENSORS_SHARED_MEM2):
        # dwSignature(4) dwVersion(4) dwRevision(4) poll_time(8=long) = 20 bytes
        # dwOffsetOfSensorSection(4)@0x14, dwSizeOfSensorElement(4)@0x18, dwNumSensorElements(4)@0x1C
        # dwOffsetOfReadingSection(4)@0x20, dwSizeOfReadingElement(4)@0x24, dwNumReadingElements(4)@0x28
        hdr = ctypes.string_at(ptr, 48)
        off_s = struct.unpack_from('<I', hdr, 0x14)[0]  # sensor (device group) section
        sz_s  = struct.unpack_from('<I', hdr, 0x18)[0]
        n_s   = struct.unpack_from('<I', hdr, 0x1C)[0]

        # Read device group names from the sensor section
        # _HWiNFO_SENSORS_SENSOR_ELEMENT: dwSensorID(4) dwSensorInst(4) szSensorNameOrig(128) szSensorNameUser(128)
        device_names = []
        for i in range(n_s):
            entry = ctypes.string_at(ptr + off_s + i * sz_s, min(sz_s, 264))
            name = entry[8:8+128].rstrip(b'\x00').decode('ascii','replace').strip()
            if name:
                device_names.append({'index': i, 'name': name})

        # dwSensorIndex linking each reading to its device group is at offset 0x04
        sensors = []
        for i in range(n_e):
            entry = ctypes.string_at(ptr + off_e + i * sz_e, sz_e)
            stype        = struct.unpack_from('<I', entry, 0x00)[0]
            sensor_idx   = struct.unpack_from('<I', entry, 0x04)[0]
            name_orig    = entry[0x0C:0x0C+128].rstrip(b'\x00').decode('ascii','replace').strip()
            unit         = entry[0x10C:0x10C+16].rstrip(b'\x00').decode('ascii','replace').strip()
            val          = struct.unpack_from('<d', entry, 0x11C)[0]
            if not name_orig: continue
            sensors.append({
                'index':        i,
                'sensor_index': sensor_idx,
                'type':         TYPE_NAMES.get(stype, 'Other'),
                'name':         name_orig,
                'unit':         unit,
                'sample':       round(val, 3),
            })

        out = {'generated': str(datetime.now()), 'device_names': device_names, 'sensors': sensors}
        sensors_path = os.path.join(CONFIG_DIR, 'hwinfo_sensors.json')
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(sensors_path, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2)
        print(f'Sensor discovery: {len(sensors)} sensors, {len(device_names)} device groups saved to {sensors_path}')
        return sensors_path
    except Exception as e:
        print(f'Sensor discovery error: {e}')
        return False

def read_sensors():
    """Read all sensor values from HWiNFO64's shared memory. Returns an
    empty dict if HWiNFO64 isn't running or shared memory isn't enabled —
    callers should treat missing keys as 'sensor unavailable', not an error."""
    # CUSTOM_N entries (manually wired sensors HWiNFO doesn't have a
    # standard name for) come from the currently loaded THEME's own
    # sensorMap, not a persisted global config -- standard keys resolve
    # by name fresh on every read inside read_sharedmem() itself and need
    # no map passed in at all.
    custom_map = {}
    if _current_theme:
        for k, v in _current_theme.get('sensorMap', {}).items():
            if k.startswith('CUSTOM_') and v is not None:
                custom_map[k] = v
    if _shm_handle is None:
        try_open_sharedmem()
    d = read_sharedmem(custom_map) if _shm_handle is not None else {}

    # RTSS is optional and independent of HWiNFO — merge in FPS values if available
    rtss_vals = read_rtss_framerate()
    if rtss_vals is not None:
        d.update(rtss_vals)

    return d

# ── RTSS Reader (optional — FPS via RivaTuner Statistics Server) ──────────────
# Independent of HWiNFO. RTSS hooks directly into the game's D3D/OpenGL/Vulkan
# present calls, so its per-process framerate is attributed correctly without
# needing HWiNFO Pro to exclude background applications.
_rtss_handle = None       # (kernel32, win_handle, ptr, size) once mapped
_rtss_unavailable_logged = False
_rtss_last_attempt = 0     # time.time() of the last connection attempt
_rtss_retry_interval = 10  # seconds between reconnect attempts while unavailable

def try_open_rtss():
    """Try to open RTSS shared memory using the same pure-ctypes approach
    that works for HWiNFO. Safe to call repeatedly — RTSS may not be
    running, may be started later, or may be closed; we just keep retrying
    on each read rather than treating one failure as permanent."""
    global _rtss_handle, _rtss_unavailable_logged
    try:
        import ctypes
        kernel32      = ctypes.windll.kernel32
        FILE_MAP_READ       = 0x0004
        FILE_MAP_ALL_ACCESS  = 0x000F001F

        kernel32.OpenFileMappingW.restype = ctypes.c_void_p
        kernel32.MapViewOfFile.restype    = ctypes.c_void_p
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes     = [ctypes.c_void_p]

        # The access mask passed to OpenFileMappingW constrains what
        # MapViewOfFile can later request against that same handle — so if
        # MapViewOfFile fails with ERROR_ACCESS_DENIED, retrying with a
        # different access right on the SAME handle won't help; we need a
        # fresh OpenFileMappingW call with a different requested access
        # right. Try every (access, name) combination as a full
        # open+map+verify cycle, closing and moving on between attempts.
        #
        # IMPORTANT: request size 0 here, not a guessed fixed size. Passing
        # a size larger than the section RTSS actually created can itself
        # cause MapViewOfFile to fail with ERROR_ACCESS_DENIED (5) — the
        # same symptom as a real permissions problem, but with a totally
        # different cause. 0 means "map the whole existing section,
        # whatever size it actually is" and is the standard safe approach
        # when you don't control the size the mapping was created with.
        ptr = None
        win_handle = None
        opened_name = None
        opened_access = None
        last_err = None

        for access in (FILE_MAP_ALL_ACCESS, FILE_MAP_READ):
            for name in ('RTSSSharedMemoryV2', 'Global\\RTSSSharedMemoryV2'):
                h = kernel32.OpenFileMappingW(access, False, name)
                if not h:
                    continue
                p = kernel32.MapViewOfFile(h, access, 0, 0, 0)
                if p:
                    ptr, win_handle, opened_name, opened_access = p, h, name, access
                    break
                last_err = kernel32.GetLastError()
                try:
                    print('  RTSS MapViewOfFile failed (error %s) on "%s" with access=0x%X - trying next combination' % (last_err, name, access), flush=True)
                except Exception as log_err:
                    print('  RTSS MapViewOfFile failed, and logging itself raised: %r' % (log_err,), flush=True)
                kernel32.CloseHandle(h)
            if ptr:
                break

        if not win_handle:
            # RTSS not running — this is a normal, expected state since RTSS
            # is an optional feature. Log once, not every frame.
            if not _rtss_unavailable_logged:
                if last_err is not None:
                    print('RTSS shared memory could not be mapped (last error %s) - '
                          'this is optional, FPS sensor will be unavailable. Make sure '
                          'RTSS (RivaTuner Statistics Server) is installed and running.' % (last_err,), flush=True)
                else:
                    print('RTSS shared memory not found (RTSS not running - this is '
                          'optional, FPS sensor will be unavailable)', flush=True)
                _rtss_unavailable_logged = True
            _rtss_handle = None
            return False

        sig_bytes = ctypes.string_at(ptr, 4)
        sig = struct.unpack('<I', sig_bytes)[0]
        # 'RTSS' as a little-endian DWORD per the SDK header
        RTSS_SIG = struct.unpack('<I', b'SSTR')[0]  # confirmed via live testing: RTSS writes the signature bytes in this order in memory
        if sig != RTSS_SIG:
            print('  RTSS signature mismatch: got 0x%08X, expected 0x%08X (0xDEAD means RTSS is shutting down)' % (sig, RTSS_SIG), flush=True)
            kernel32.UnmapViewOfFile(ptr)
            kernel32.CloseHandle(win_handle)
            _rtss_handle = None
            return False

        version = struct.unpack_from('<I', ctypes.string_at(ptr+4, 4))[0]
        if version < 0x00020000:
            # Older v1.x struct doesn't have per-app entries we need
            print('  RTSS version 0x%08X is older than v2.0 - per-app data unavailable' % (version,), flush=True)
            kernel32.UnmapViewOfFile(ptr)
            kernel32.CloseHandle(win_handle)
            _rtss_handle = None
            return False

        _rtss_handle = (kernel32, win_handle, ptr, None)
        _rtss_unavailable_logged = False
        print('RTSS shared memory connected OK (mapping="%s", version=0x%08X)' % (opened_name, version), flush=True)
        return True

    except Exception as e:
        if not _rtss_unavailable_logged:
            print('RTSS shared memory unavailable: %r' % (e,), flush=True)
            _rtss_unavailable_logged = True
        _rtss_handle = None
        return False


def list_rtss_apps():
    """Return a list of (process_id, exe_name, framerate) for all active
    3D applications currently tracked by RTSS. Used by Settings to let the
    user pick a specific process instead of relying on auto-detection."""
    global _rtss_handle
    apps = []
    if _rtss_handle is None:
        if not try_open_rtss():
            return apps
    try:
        import ctypes
        kernel32, win_handle, ptr, size = _rtss_handle

        # Re-verify signature hasn't gone stale (RTSS could have shut down
        # since we last opened the mapping)
        hdr = ctypes.string_at(ptr, 36)
        sig = struct.unpack_from('<I', hdr, 0)[0]
        RTSS_SIG = struct.unpack('<I', b'SSTR')[0]  # confirmed via live testing: RTSS writes the signature bytes in this order in memory
        if sig != RTSS_SIG:
            try:
                kernel32.UnmapViewOfFile(ptr)
                kernel32.CloseHandle(win_handle)
            except Exception:
                pass
            _rtss_handle = None
            return apps

        # v2.0 header layout (RTSS_SHARED_MEMORY):
        # dwSignature(4) dwVersion(4) dwAppEntrySize(4) dwAppArrOffset(4)
        # dwAppArrSize(4) dwOSDEntrySize(4) dwOSDArrOffset(4) dwOSDArrSize(4)
        # dwOSDFrame(4) = 36 bytes, then arrOSD[8], then arrApp[256]
        app_entry_size = struct.unpack_from('<I', hdr, 8)[0]
        app_arr_offset = struct.unpack_from('<I', hdr, 12)[0]

        if app_entry_size <= 0 or app_entry_size > 100000:
            return apps  # sanity check — malformed/unexpected layout

        for i in range(256):
            entry_ptr = ptr + app_arr_offset + i*app_entry_size
            # dwProcessID(4) at offset 0, szName[MAX_PATH=260] at offset 4
            pid_bytes = ctypes.string_at(entry_ptr, 4)
            pid = struct.unpack('<I', pid_bytes)[0]
            if pid == 0:
                continue  # empty slot
            name_bytes = ctypes.string_at(entry_ptr + 4, 260)
            name = name_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
            if not name:
                continue

            # dwStatFramerateAvg (offset 308) turned out to be tied to RTSS's
            # benchmark/stat-recording session lifecycle — it can spike
            # absurdly high right as a session starts, then drop to 0 once
            # that recording window ends, since it's not continuously live.
            # dwStatFrameTimeBufFramerate (offset 5024, v2.5+) is the value
            # backing RTSS's own ring buffer of recent frametimes — it
            # updates continuously every frame with no recording-session
            # concept, which is what working third-party readers like
            # CapFrameX use. Stored in units of 0.1 FPS. Fall back to the
            # older instantaneous dwFrameTime-based calc on RTSS versions
            # too old to have this field at all.
            if app_entry_size >= 5028:
                favg_bytes = ctypes.string_at(entry_ptr + 5024, 4)
                fps = struct.unpack('<I', favg_bytes)[0] / 10.0
            else:
                frametime_bytes = ctypes.string_at(entry_ptr + 280, 4)
                frame_time_us = struct.unpack('<I', frametime_bytes)[0]
                fps = (1000000.0 / frame_time_us) if frame_time_us > 0 else 0.0

            apps.append((pid, name, round(fps, 1)))

    except Exception as e:
        print('RTSS app list error: %r' % (e,), flush=True)
    return apps


def read_rtss_framerate():
    """Return the current framerate (float) from RTSS, or None if RTSS
    isn't running / no active 3D app is detected. Optional feature —
    callers should treat None as 'sensor unavailable', not an error.

    Selection behavior:
      - cfg['rtss_process'] set (non-empty) -> match that exe name exactly
      - otherwise -> auto-pick the active app: the entry with the most
        recently updated dwTime1, which corresponds to whichever hooked
        3D application most recently rendered a frame (i.e. the one
        currently in the foreground / actively rendering)
    """
    global _rtss_handle, _rtss_last_attempt
    if _rtss_handle is None:
        now = time.time()
        if now - _rtss_last_attempt < _rtss_retry_interval:
            return None  # still cooling down since the last failed attempt
        _rtss_last_attempt = now
        if not try_open_rtss():
            return None
    try:
        import ctypes
        kernel32, win_handle, ptr, size = _rtss_handle

        hdr = ctypes.string_at(ptr, 36)
        sig = struct.unpack_from('<I', hdr, 0)[0]
        RTSS_SIG = struct.unpack('<I', b'SSTR')[0]  # confirmed via live testing: RTSS writes the signature bytes in this order in memory
        if sig != RTSS_SIG:
            # Signature flipped to 0xDEAD (or similar) — RTSS shut down while
            # we were holding the mapping open. Drop our handle so the next
            # call to try_open_rtss() actually attempts a fresh connection
            # instead of silently reusing a dead one forever.
            try:
                kernel32.UnmapViewOfFile(ptr)
                kernel32.CloseHandle(win_handle)
            except Exception:
                pass
            _rtss_handle = None
            return None

        app_entry_size = struct.unpack_from('<I', hdr, 8)[0]
        app_arr_offset = struct.unpack_from('<I', hdr, 12)[0]
        if app_entry_size <= 0 or app_entry_size > 100000:
            return None

        target_name = cfg.get('rtss_process', '').strip().lower()

        best_pid = None
        best_time1 = -1
        best_fps = None
        best_entry_ptr = None

        for i in range(256):
            entry_ptr = ptr + app_arr_offset + i*app_entry_size
            pid_bytes = ctypes.string_at(entry_ptr, 4)
            pid = struct.unpack('<I', pid_bytes)[0]
            if pid == 0:
                continue

            name_bytes = ctypes.string_at(entry_ptr + 4, 260)
            name = name_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='replace')

            time1_bytes = ctypes.string_at(entry_ptr + 272, 4)
            time1 = struct.unpack('<I', time1_bytes)[0]

            # dwStatFrameTimeBufFramerate (offset 5024, v2.5+): backed by
            # RTSS's continuously-updating ring buffer of recent
            # frametimes, with no recording-session lifecycle — unlike
            # dwStatFramerateAvg, this won't spike then drop to zero.
            # Stored in units of 0.1 FPS. Fall back to the older
            # instantaneous dwFrameTime-based calc on RTSS versions too old
            # to have this field at all.
            if app_entry_size >= 5028:
                favg_bytes = ctypes.string_at(entry_ptr + 5024, 4)
                fps = struct.unpack('<I', favg_bytes)[0] / 10.0
            else:
                frametime_bytes = ctypes.string_at(entry_ptr + 280, 4)
                frame_time_us = struct.unpack('<I', frametime_bytes)[0]
                fps = (1000000.0 / frame_time_us) if frame_time_us > 0 else 0.0

            if target_name:
                if name.lower() == target_name:
                    return _build_rtss_result(entry_ptr, app_entry_size, fps, ctypes)
                continue

            # Auto mode: pick whichever app most recently rendered a frame
            if time1 > best_time1:
                best_time1 = time1
                best_pid = pid
                best_fps = fps
                best_entry_ptr = entry_ptr

        if target_name:
            return None  # configured process not currently found/running
        if best_fps is None:
            return None
        return _build_rtss_result(best_entry_ptr, app_entry_size, best_fps, ctypes)

    except Exception as e:
        print('RTSS read error: %r' % (e,), flush=True)
        return None

def _build_rtss_result(entry_ptr, app_entry_size, fps, ctypes):
    """Build the RTSS sensor dict from an app entry pointer.
    Returns dict with RTSS_FPS, RTSS_FPS_MIN, RTSS_FPS_MAX, RTSS_FPS_AVG."""
    result = {'RTSS_FPS': round(fps, 1)}
    try:
        # dwStatFramerateMin @304, dwStatFramerateAvg @308, dwStatFramerateMax @312
        # These are session-based averages but still useful for display when available.
        # They reset with each benchmark session but give min/max context when non-zero.
        if app_entry_size >= 316:
            fmin = struct.unpack('<I', ctypes.string_at(entry_ptr + 304, 4))[0]
            favg = struct.unpack('<I', ctypes.string_at(entry_ptr + 308, 4))[0]
            fmax = struct.unpack('<I', ctypes.string_at(entry_ptr + 312, 4))[0]
            # Only expose these if they have plausible non-zero values
            if fmin > 0: result['RTSS_FPS_MIN'] = float(fmin)
            if favg > 0: result['RTSS_FPS_AVG'] = float(favg)
            if fmax > 0: result['RTSS_FPS_MAX'] = float(fmax)
    except Exception:
        pass
    return result

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
                fmt  = el.get('clockFormat','12h')
                secs = el.get('clockSeconds', True)
                if fmt=='12h':
                    if secs:
                        # With seconds: zero-pad hour so AM/PM stays stable
                        text = now.strftime('%I:%M:%S %p')
                    else:
                        # No seconds: strip leading zero, no shifting issue
                        text = now.strftime('%I:%M %p').lstrip('0')
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
            style   = el.get('barStyle','solid')

            if style == 'segmented':
                # Discrete LED-style blocks — each segment is either fully lit or unlit
                segs    = int(el.get('segmentCount', 12))
                gap_pct = float(el.get('segmentGap', 18)) / 100.0
                lit_count = round(pct * segs)
                seg_w_full = w / segs
                seg_w = seg_w_full * (1 - gap_pct)
                seg_rad = min(rad, 3)
                for i in range(segs):
                    sx = x + i*seg_w_full
                    lit = i < lit_count
                    color = fill_c if lit else bg_c
                    if seg_rad > 0:
                        draw.rounded_rectangle([sx, y, sx+seg_w, y+h], radius=seg_rad, fill=color)
                    else:
                        draw.rectangle([sx, y, sx+seg_w, y+h], fill=color)

            elif style == 'gapped':
                # Continuous fill with thin vertical gap lines overlaid
                segs  = int(el.get('segmentCount', 16))
                gap_w = max(1, int(el.get('segmentGap', 2)))
                if rad > 0:
                    draw.rounded_rectangle([x,y,x+w,y+h], radius=rad, fill=bg_c)
                else:
                    draw.rectangle([x,y,x+w,y+h], fill=bg_c)
                fw = int(w*pct)
                if fw > 0:
                    if rad > 0:
                        draw.rounded_rectangle([x,y,x+fw,y+h], radius=rad, fill=fill_c)
                    else:
                        draw.rectangle([x,y,x+fw,y+h], fill=fill_c)
                # Overlay gap lines using the background color, cutting through both fill and empty zones
                seg_w_full = w / segs
                for i in range(1, segs):
                    gx = x + i*seg_w_full
                    draw.rectangle([gx-gap_w/2, y, gx+gap_w/2, y+h], fill=bg_c)
                if thick > 0:
                    draw.rectangle([x,y,x+w,y+h], outline=bord_c, width=thick)

            else:  # solid
                if rad > 0:
                    draw.rounded_rectangle([x,y,x+w,y+h], radius=rad, fill=bg_c)
                else:
                    draw.rectangle([x,y,x+w,y+h], fill=bg_c)
                fw = int(w*pct)
                if fw > 0:
                    if rad > 0:
                        draw.rounded_rectangle([x,y,x+fw,y+h], radius=rad, fill=fill_c)
                    else:
                        draw.rectangle([x,y,x+fw,y+h], fill=fill_c)
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
            ring_style = el.get('ringStyle', 'solid')

            if ring_style == 'segmented':
                # Discrete arc blocks all the way around; lit segments use arc_c,
                # unlit use trk_c. Matches the builder's SVG segmented preview.
                segs    = int(el.get('segmentCount', 24))
                gap_deg = float(el.get('segmentGap', 6))
                seg_deg = 360.0 / segs
                arc_deg = max(0.5, seg_deg - gap_deg)
                lit_count = round(pct * segs)
                for i in range(segs):
                    start_a = -90 + i*seg_deg + gap_deg/2
                    end_a   = start_a + arc_deg
                    lit     = i < lit_count
                    color   = arc_c if lit else trk_c
                    draw.arc(box, start_a, end_a, fill=color, width=rw)
            else:
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
            eid     = el.get('id','')
            hist_s  = int(el.get('historySeconds', 60))
            maxlen  = max(10, hist_s * cfg.get('fps',6))

            # Build series list: series 1 = left axis, series 2/3 = right axis
            max_val  = float(el.get('maxValue', 100))
            max_val2 = float(el.get('maxValue2', 100))
            series_defs = [
                (el.get('sensorKey',''),  el.get('lineColor','#00b4ffff'),  max_val,  el.get('fillColor')),
            ]
            if el.get('sensorKey2'):
                series_defs.append((el.get('sensorKey2'), el.get('lineColor2','#ff3df0ff'), max_val2, None))
            if el.get('sensorKey3'):
                series_defs.append((el.get('sensorKey3'), el.get('lineColor3','#5effc0ff'), max_val2, None))

            lw   = int(el.get('lineWidth',2))
            rad  = int(el.get('cornerRadius',4))
            bg_c = color_rgba(el.get('bgColor','#0a0a1499'))
            gc   = color_rgba(el.get('gridColor','#ffffff22'))
            show_grid = el.get('showGrid', True)

            if rad>0: draw.rounded_rectangle([x,y,x+w,y+h],radius=rad,fill=bg_c)
            else:     draw.rectangle([x,y,x+w,y+h],fill=bg_c)

            if show_grid:
                for gi in range(1,4):
                    gy = y + h*gi//4
                    draw.line([(x,gy),(x+w,gy)], fill=gc, width=1)

            for si, (skey, lcolor, smax, fillcolor) in enumerate(series_defs):
                if not skey: continue
                raw_val = float(sv(skey, 0))
                hist_key = f'{eid}_{si}'
                if hist_key not in _graph_history:
                    from collections import deque
                    _graph_history[hist_key] = deque(maxlen=maxlen)
                _graph_history[hist_key].append(raw_val)
                pts = list(_graph_history[hist_key])
                if len(pts) < 2: continue

                lc = color_rgb(lcolor)
                n = len(pts)
                def gx(i): return x + int(i/(n-1)*w)
                def gy_v(v, smax=smax): return y+h - int(max(0,min(1,v/smax if smax else 0))*h*0.88+h*0.06)
                coords = [(gx(i), gy_v(v)) for i,v in enumerate(pts)]

                # Only series 1 gets a fill-under (matches builder preview behavior)
                if si == 0 and fillcolor:
                    fc = color_rgba(fillcolor)
                    poly = [(x,y+h)] + coords + [(x+w,y+h)]
                    draw.polygon(poly, fill=fc)

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
                header, b64 = bg_data_url.split(',', 1)
                if 'svg' in header.lower():
                    print('Background image is SVG format, which Pillow cannot decode — '
                          'this theme was likely generated by an older version of the AI '
                          'Theme Generator. Re-generate and re-save the theme in the theme '
                          'builder to fix (newer versions export a PNG background instead).')
                else:
                    try:
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

        # Standard sensor keys (CPU_USAGE, GPU_TEMP, etc.) need no mapping
        # step at all -- read_sharedmem() resolves them fresh by NAME on
        # every single read. Any CUSTOM_N entries in this theme's own
        # sensorMap are read directly from _current_theme by read_sensors()
        # when needed, with nothing copied into the global config.

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

def detect_ds916_port():
    """Scan serial ports for a device matching the DS916 VID/PID (33C3:F101).
    Returns the port name if found, otherwise None."""
    try:
        for port in serial.tools.list_ports.comports():
            # pyserial exposes VID/PID on the port info
            if port.vid == 0x33C3 and port.pid == 0xF101:
                print(f'DS916 auto-detected on {port.device} (VID=33C3 PID=F101)')
                return port.device
            # Also check the hardware ID string as fallback
            hwid = (port.hwid or '').upper()
            if 'VID_33C3' in hwid and 'PID_F101' in hwid:
                print(f'DS916 auto-detected on {port.device} via HWID match')
                return port.device
    except Exception as e:
        print(f'Port detection error: {e}')
    return None

def open_port():
    global _port
    try:
        if _port and _port.is_open: _port.close()
        # Auto-detect DS916 port; fall back to config value
        detected = detect_ds916_port()
        if detected and detected != cfg['com_port']:
            print(f'Updating COM port: {cfg["com_port"]} → {detected}')
            cfg['com_port'] = detected
            save_cfg(cfg)
        port_to_use = cfg['com_port']
        _port = serial.Serial(port_to_use, baudrate=115200, timeout=2)
        print(f'Opened {port_to_use}')
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
    ttk.Combobox(t1, textvariable=com_var, values=ports, width=10, state='readonly').grid(
        row=0, column=1, sticky='w', padx=8, pady=4)
    def auto_detect_port():
        found = detect_ds916_port()
        if found:
            com_var.set(found)
            toast_lbl.config(text=f'✅ DS916 found on {found}', foreground='#4fc87a')
        else:
            toast_lbl.config(text='DS916 not found — is it plugged in?', foreground='#e05a4b')
    ttk.Button(t1, text='Auto-detect', command=auto_detect_port).grid(
        row=0, column=2, padx=4, pady=4)
    toast_lbl = ttk.Label(t1, text='', font=('Segoe UI', 8), foreground='#4fc87a')
    toast_lbl.grid(row=1, column=0, columnspan=3, sticky='w', padx=8)

    lbl(t1, 'FPS:', 2)
    fps_var = tk.IntVar(value=cfg.get('fps', 6))
    ttk.Spinbox(t1, from_=1, to=30, textvariable=fps_var, width=6).grid(
        row=2, column=1, sticky='w', padx=8, pady=4)

    lbl(t1, 'Theme File:', 3)
    theme_var = tk.StringVar(value=cfg.get('theme_path', ''))
    ttk.Entry(t1, textvariable=theme_var, width=28).grid(
        row=3, column=1, sticky='ew', padx=8, pady=4)
    def browse_theme():
        p = filedialog.askopenfilename(
            parent=win, filetypes=[('DS916 Theme', '*.ds916theme *.zip')])
        if p: theme_var.set(p)
    ttk.Button(t1, text='Browse…', command=browse_theme).grid(row=3, column=2, padx=4, pady=4)

    auto_var = tk.BooleanVar(value=cfg.get('autostart', True))
    ttk.Checkbutton(t1, text='Start display automatically with Windows',
                    variable=auto_var).grid(row=4, column=0, columnspan=3, sticky='w', padx=8, pady=6)

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
        from datetime import datetime, timedelta

        # Align the first restart with HWiNFO64's actual process start time,
        # not the moment Install was clicked — these can differ by hours if
        # HWiNFO64 was already running for a while before this task gets set
        # up, which would otherwise leave a real gap where shared memory is
        # dead before the task ever gets a chance to restart it.
        hwinfo_start = None
        try:
            import ctypes
            from ctypes import wintypes
            psapi = ctypes.windll.psapi
            kernel32 = ctypes.windll.kernel32
            # Enumerate processes and find HWiNFO64.exe's start time via its PID
            pids = (wintypes.DWORD * 1024)()
            cb_needed = wintypes.DWORD()
            psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(cb_needed))
            count = cb_needed.value // ctypes.sizeof(wintypes.DWORD)
            PROCESS_QUERY_INFORMATION = 0x0400
            for i in range(count):
                pid = pids[i]
                if not pid: continue
                hproc = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
                if not hproc: continue
                try:
                    name_buf = ctypes.create_unicode_buffer(260)
                    size = wintypes.DWORD(260)
                    if psapi.GetModuleBaseNameW(hproc, None, name_buf, size):
                        if name_buf.value.lower() == 'hwinfo64.exe':
                            creation = wintypes.FILETIME()
                            exit_t = wintypes.FILETIME()
                            kernel_t = wintypes.FILETIME()
                            user_t = wintypes.FILETIME()
                            if kernel32.GetProcessTimes(hproc, ctypes.byref(creation),
                                    ctypes.byref(exit_t), ctypes.byref(kernel_t), ctypes.byref(user_t)):
                                ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                                # FILETIME is 100ns intervals since 1601-01-01
                                hwinfo_start = datetime(1601,1,1) + timedelta(microseconds=ft/10)
                finally:
                    kernel32.CloseHandle(hproc)
                if hwinfo_start: break
        except Exception as e:
            print(f'Could not detect HWiNFO64 start time: {e}')

        if hwinfo_start:
            # First restart at HWiNFO_start + 11.5h. If that moment has
            # already passed (HWiNFO has been up 11.5h+ already), schedule
            # the first run very soon instead of waiting a further 11.5h.
            first_run = hwinfo_start + timedelta(hours=11, minutes=30)
            if first_run <= datetime.now():
                first_run = datetime.now() + timedelta(minutes=2)
            start_dt = first_run.strftime('%Y-%m-%dT%H:%M:%S')
            print(f'Aligning restart task to HWiNFO64 start time {hwinfo_start} -> first run {first_run}')
        else:
            # Couldn't detect HWiNFO64's start time (not running, or detection
            # failed) — fall back to 11.5h from now as before.
            start_dt = (datetime.now() + timedelta(hours=11, minutes=30)).strftime('%Y-%m-%dT%H:%M:%S')
            print('Could not detect HWiNFO64 process start time — using 11.5h from now as fallback')

        # Task: TimeTrigger starting at the aligned time, repeating every 690 minutes indefinitely
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT690M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start_dt}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-WindowStyle Hidden -Command "Stop-Process -Name HWiNFO64 -Force -ErrorAction SilentlyContinue; Start-Sleep 3; Start-Process '{path}' -ArgumentList '-sensors'"</Arguments>
    </Exec>
  </Actions>
  <Settings>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Hidden>false</Hidden>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
</Task>'''
        xml_path = os.path.join(os.environ.get('TEMP','C:\\Temp'), 'ds916_hwinfo_task.xml')
        with open(xml_path, 'w', encoding='utf-16') as f: f.write(xml)

        # schtasks /create requires elevation — use ShellExecuteW with 'runas'
        # so Windows shows a UAC prompt rather than silently failing with
        # "Access is denied". We wait for the elevated process to finish,
        # then check whether the task now exists to determine success.
        import ctypes
        args = f'/create /tn DS916_HWiNFO_Restart /xml "{xml_path}" /f'
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', 'schtasks.exe', args, None, 0)  # SW_HIDE=0

        # ShellExecuteW returns >32 on success (process launched); <=32 is an error
        # (including 5=access denied if user cancelled UAC, 2=not found, etc.)
        if ret <= 32:
            try: os.unlink(xml_path)
            except Exception: pass
            if ret == 5:
                messagebox.showerror('DS916',
                    'UAC prompt was cancelled.\n\nElevation is required to create a Scheduled Task.',
                    parent=win)
            else:
                messagebox.showerror('DS916',
                    f'Failed to launch schtasks (ShellExecute error {ret}).',
                    parent=win)
            return

        # Wait a moment for the elevated schtasks process to finish, then
        # verify by checking whether the task now exists
        import time
        time.sleep(2)
        try: os.unlink(xml_path)
        except Exception: pass

        if task_exists():
            cfg['hwinfo_path'] = path
            save_cfg(cfg)
            messagebox.showinfo('DS916',
                'Scheduled task created!\n\nHWiNFO64 will restart every 11.5 hours\nto keep shared memory active.', parent=win)
            update_task_btn()
        else:
            messagebox.showerror('DS916',
                'Task creation may have failed — the task was not found after install.\n'
                'Try running the tray app as Administrator if the UAC prompt did not appear.',
                parent=win)

    def remove_task():
        import ctypes, time
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', 'schtasks.exe',
            '/delete /tn DS916_HWiNFO_Restart /f', None, 0)
        if ret <= 32:
            if ret != 5:  # 5 = user cancelled UAC, silent is fine
                messagebox.showerror('DS916',
                    f'Failed to remove task (ShellExecute error {ret}).', parent=win)
            return
        time.sleep(2)
        if not task_exists():
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

    # ── Tab: RTSS (optional FPS source) ──────────────────────────────────────
    t4 = ttk.Frame(nb); nb.add(t4, text='  RTSS (FPS)  ')
    ttk.Label(t4, text='RivaTuner Statistics Server (RTSS) is an optional, separate source for\n'
                        'reliable per-game FPS — independent of HWiNFO. If RTSS isn\'t installed\n'
                        'or running, the FPS sensor simply stays unavailable; everything else\n'
                        'keeps working normally.',
              font=('Segoe UI', 9), foreground='#888', justify='left').pack(anchor='w', padx=10, pady=(8,8))

    rtss_status_lbl = ttk.Label(t4, text='Checking...', font=('Segoe UI', 9))
    rtss_status_lbl.pack(anchor='w', padx=10, pady=(0,8))

    mode_frame = ttk.Frame(t4); mode_frame.pack(anchor='w', padx=10, pady=4, fill='x')
    rtss_mode_var = tk.StringVar(value='auto' if not cfg.get('rtss_process','').strip() else 'manual')
    def on_mode_change():
        is_manual = rtss_mode_var.get()=='manual'
        proc_cb.config(state='readonly' if is_manual else 'disabled')
    ttk.Radiobutton(mode_frame, text='Auto-detect active 3D app (recommended)', value='auto',
                    variable=rtss_mode_var, command=on_mode_change).pack(anchor='w')
    ttk.Radiobutton(mode_frame, text='Pin a specific process:', value='manual',
                    variable=rtss_mode_var, command=on_mode_change).pack(anchor='w', pady=(4,0))

    proc_row = ttk.Frame(t4); proc_row.pack(anchor='w', padx=28, pady=(2,8), fill='x')
    proc_var = tk.StringVar(value=cfg.get('rtss_process',''))
    proc_cb = ttk.Combobox(proc_row, textvariable=proc_var, width=30,
                           state='readonly' if rtss_mode_var.get()=='manual' else 'disabled')
    proc_cb.pack(side='left')

    def refresh_rtss_apps():
        apps = list_rtss_apps()
        if apps:
            names = sorted(set(name for _,name,_ in apps))
            proc_cb.config(values=names)
            rtss_status_lbl.config(
                text=f'✅ RTSS connected — {len(apps)} active 3D app(s) detected',
                foreground='#4fc87a')
        else:
            proc_cb.config(values=[])
            if _rtss_handle is None:
                rtss_status_lbl.config(
                    text='○ RTSS not running (optional — install from guru3d.com if you want FPS)',
                    foreground='#888')
            else:
                rtss_status_lbl.config(
                    text='✅ RTSS connected — no active 3D app detected right now',
                    foreground='#4fc87a')
    ttk.Button(proc_row, text='↻ Refresh List', command=refresh_rtss_apps).pack(side='left', padx=(6,0))
    refresh_rtss_apps()

    # ── Save / Cancel ─────────────────────────────────────────────────────────
    def save_settings():
        cfg['com_port']      = com_var.get()
        cfg['fps']           = fps_var.get()
        cfg['theme_path']    = theme_var.get()
        cfg['autostart']     = auto_var.get()
        cfg['rtss_process']  = proc_var.get().strip() if rtss_mode_var.get()=='manual' else ''
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
    # theme_builder.html now lives in CONFIG_DIR (AppData), installed there
    # by build.bat alongside the exe — not next to wherever the exe happens
    # to be run from. This keeps everything for this app in one place.
    html = os.path.join(CONFIG_DIR, 'theme_builder.html')
    if os.path.exists(html):
        webbrowser.open('file:///'+html.replace('\\','/'))
    else:
        _dispatch(lambda: messagebox.showwarning('DS916',
            'theme_builder.html not found in:\n'+CONFIG_DIR+
            '\n\nIf you built this app yourself, re-run build.bat to reinstall it there.'))

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
        '  • Remove the Desktop and Start Menu shortcuts\n'
        '  • Delete theme_builder.html, config, and discovered sensor data\n'
        '  • Close the tray app\n\n'
        'Your theme files (.ds916theme) will NOT be deleted.\n\n'
        'Note: DS916Tray.exe itself can\'t delete itself while running — '
        'it will be left behind in the (now otherwise empty) install folder. '
        'You can delete it manually once the app has closed.\n\n'
        'Continue?',
        parent=_tk_root
    )
    if not result:
        return

    # 1. Remove from startup
    set_autostart(False)

    # 2. Remove Desktop and Start Menu shortcuts created by build.bat
    try:
        import ctypes.wintypes
        CSIDL_DESKTOPDIRECTORY = 0x10
        CSIDL_PROGRAMS = 0x02
        buf = ctypes.create_unicode_buffer(260)
        shortcut_dirs = []
        for csidl in (CSIDL_DESKTOPDIRECTORY, CSIDL_PROGRAMS):
            ctypes.windll.shell32.SHGetFolderPathW(0, csidl, 0, 0, buf)
            shortcut_dirs.append(buf.value)
        for d in shortcut_dirs:
            lnk = os.path.join(d, 'DS916 Tray.lnk')
            if os.path.exists(lnk):
                os.unlink(lnk)
                print(f'Removed shortcut: {lnk}')
    except Exception as e:
        print(f'Shortcut removal error: {e}')

    # 3. Delete everything in CONFIG_DIR (theme_builder.html, config, sensors)
    # EXCEPT the running exe itself (Windows won't let us delete it while
    # it's open — left behind harmlessly) and the Themes subfolder (the
    # dialog explicitly promises these won't be deleted).
    import shutil
    if os.path.exists(CONFIG_DIR):
        exe_name = 'DS916Tray.exe'
        themes_name = os.path.basename(THEMES_DIR)
        for entry in os.listdir(CONFIG_DIR):
            if entry == exe_name or entry == themes_name:
                continue
            full = os.path.join(CONFIG_DIR, entry)
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
                print(f'Removed: {full}')
            except Exception as e:
                print(f'Removal error for {full}: {e}')

    # 4. Clean up temp font files
    for path in _custom_font_files.values():
        try:
            if os.path.exists(path):
                os.unlink(path)
        except: pass

    messagebox.showinfo(
        'DS916 Tray — Uninstalled',
        'DS916 Tray has been removed from startup, and shortcuts and '
        'data have been deleted.\n\n'
        'DS916Tray.exe itself is left in:\n'+CONFIG_DIR+
        '\n\nYou can delete that file manually now that the app is closing.',
        parent=_tk_root
    )

    # 5. Exit
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
    win.configure(bg='#0f0f11')
    win.resizable(True, True)
    win.minsize(380, 360)
    win.withdraw()  # hide until sized correctly
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
    if _shm_handle is not None:
        src_label = 'Shared Memory  ✅'
        src_col   = GRN
    else:
        src_label = 'Unavailable — HWiNFO64 not running or Shared Memory Support not enabled'
        src_col   = '#d4b84a'
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

    # Spacer at bottom of scroll area
    tk.Frame(inner, bg=BG, height=8).pack()

    # Buttons outside scroll area (always visible at bottom)
    btn_frame = tk.Frame(win, bg=BG)
    btn_frame.pack(fill='x', padx=12, pady=8, side='bottom')
    tk.Button(btn_frame, text='↻ Refresh', bg=PAN, fg=ACC,
              font=('Segoe UI', 9), bd=0, padx=12, pady=4,
              command=lambda: [win.destroy(), _show_status_main()]).pack(side='left')
    tk.Button(btn_frame, text='Close', bg=PAN, fg=TXT,
              font=('Segoe UI', 9), bd=0, padx=12, pady=4,
              command=win.destroy).pack(side='right')

    # Size and show — build was hidden so no flash
    win.update_idletasks()
    screen_h = win.winfo_screenheight()
    screen_w = win.winfo_screenwidth()
    content_h = (inner.winfo_reqheight() + hdr.winfo_reqheight() +
                 btn_frame.winfo_reqheight() + 40)
    final_h = min(content_h, int(screen_h * 0.9))
    final_w = min(460, int(screen_w * 0.9))
    x = (screen_w - final_w) // 2
    y = (screen_h - final_h) // 2
    win.geometry(f'{final_w}x{final_h}+{x}+{y}')
    win.maxsize(int(screen_w * 0.95), int(screen_h * 0.95))
    win.deiconify()  # show now that size is correct
    win.lift()
    win.focus_force()

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

    # Try shared memory at startup. If it's not available yet (HWiNFO64 not
    # running, or Shared Memory Support not enabled), don't treat this as
    # fatal — read_sensors() retries on every sensor read, so it'll connect
    # automatically as soon as HWiNFO64 becomes available.
    if try_open_sharedmem():
        print('HWiNFO source: Shared Memory ✅')
        # Auto-discover and save sensors on every startup
        discover_sensors()
    else:
        print('HWiNFO source: unavailable for now — will keep retrying on each sensor read')
        print('  (start HWiNFO64 with Settings -> General -> Shared Memory Support enabled)')

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
