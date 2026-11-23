#!/usr/bin/env python3
"""
PixelPress
Full-featured cross-platform HAP video encoder (macOS + Windows).
"""

import subprocess, threading, shutil, os, re, queue, shlex, sys, argparse
import json
import math, time, platform, csv, uuid, signal, tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, Future

IS_WIN = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'

if IS_WIN:
    # PixelPress.exe is a windowed program. When it has no console (started from Explorer, or by
    # another program such as Smart Preso), every ffmpeg / ffprobe / pphap it starts would open a
    # black console window of its own - so they are started without one. In a console (the
    # command line, pixelpress.com) they share it, so Ctrl+C and their messages work as usual.
    import ctypes
    def _has_console() -> bool:
        try:
            return bool(ctypes.windll.kernel32.GetConsoleWindow())
        except Exception:
            return False
    _popen_init = subprocess.Popen.__init__
    def _popen_no_window(self, *a, **kw):
        if 'creationflags' not in kw and not _has_console():
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        _popen_init(self, *a, **kw)
    subprocess.Popen.__init__ = _popen_no_window

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog
    _GUI_AVAILABLE = True
except ImportError:
    _GUI_AVAILABLE = False
    class _NoGUI:          # lets the command-line modes run without customtkinter / tkinter
        def __getattr__(self, name):
            return object
    ctk = tk = _NoGUI()
try:
    from tkinterdnd2 import TkinterDnD      # drag and drop onto the window (optional)
except Exception:
    TkinterDnD = None

# macOS: customtkinter keeps one hidden Tk menu per dropdown (CTkOptionMenu) for as long as the app
# runs, and recent macOS versions show each idle Tk menu as an empty window ("!dropdownmenu" boxes).
# So on a Mac the menu is only created when a dropdown is clicked, and removed again once it has
# closed (on macOS, posting a Tk menu returns when the menu is dismissed).
if IS_MAC and _GUI_AVAILABLE:
    try:
        import customtkinter.windows.widgets.ctk_optionmenu as _ctk_optionmenu
        _RealDropdownMenu = _ctk_optionmenu.DropdownMenu

        class _OnDemandDropdownMenu:
            def __init__(self, master=None, **kwargs):
                self._master, self._kwargs, self._menu, self._pending = master, dict(kwargs), None, None

            def _real(self):
                if self._pending is not None:
                    self._master.after_cancel(self._pending)
                    self._pending = None
                if self._menu is None:
                    self._menu = _RealDropdownMenu(master=self._master, **self._kwargs)
                return self._menu

            def _release(self):
                self._pending = None
                if self._menu is None:
                    return
                try:
                    if self._menu.is_open():                 # still showing: look again shortly
                        self._pending = self._master.after(500, self._release)
                        return
                    self._menu.destroy()
                except Exception:
                    pass
                self._menu = None

            def open(self, x, y):
                self._real().open(x, y)
                # after the menu's command has run (it is delivered after the menu closes)
                self._pending = self._master.after(400, self._release)

            def close(self):
                if self._menu is not None:
                    self._menu.close()

            def is_open(self):
                try:
                    return self._menu is not None and bool(self._menu.is_open())
                except Exception:
                    return False

            def configure(self, **kwargs):
                self._kwargs.update(kwargs)
                if self._menu is not None:
                    self._menu.configure(**kwargs)
            config = configure

            def cget(self, name):
                if self._menu is not None:
                    return self._menu.cget(name)
                return self._kwargs.get(name)

            def destroy(self):
                if self._pending is not None:
                    try:
                        self._master.after_cancel(self._pending)
                    except Exception:
                        pass
                self._pending = None
                if self._menu is not None:
                    try:
                        self._menu.destroy()
                    except Exception:
                        pass
                self._menu = None

        _ctk_optionmenu.DropdownMenu = _OnDemandDropdownMenu
    except Exception:
        pass

VERSION = '1.5.24'

SETTINGS_FILE = Path.home() / '.pixelpress_settings.json'
PRESETS_FILE  = Path.home() / '.pixelpress_presets.json'

if IS_WIN:
    # C:\Program Files\PixelPress: where the PixelPress HAP plugin installer puts pphap / ffmpeg
    BREW_PATHS = [os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'PixelPress')]
else:
    BREW_PATHS = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin']
EXE = '.exe' if IS_WIN else ''
MOD_KEY   = 'Control' if IS_WIN else 'Command'   # keyboard shortcut modifier
MOD_LABEL = 'Ctrl+' if IS_WIN else '⌘'
RETURN_LABEL = 'Enter' if IS_WIN else '↩'
MONO_FONT = 'Consolas' if IS_WIN else 'Menlo'

def quote_cmd(cmd) -> str:
    """A command line as the user's shell would type it (for logs and the job details)."""
    return subprocess.list2cmdline(list(cmd)) if IS_WIN else ' '.join(shlex.quote(c) for c in cmd)

def quote_arg(a: str) -> str:
    return subprocess.list2cmdline([a]) if IS_WIN else shlex.quote(a)

def open_path(path: str):
    """Open a file or folder the way double-clicking it would."""
    try:
        if IS_WIN:
            os.startfile(path)
        elif IS_MAC:
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass

def reveal_path(path: str):
    """Show a file selected in Finder / Explorer."""
    try:
        if IS_WIN:
            subprocess.Popen(['explorer', '/select,', os.path.normpath(path)], creationflags=0)
        elif IS_MAC:
            subprocess.Popen(['open', '-R', path])
        else:
            open_path(os.path.dirname(path))
    except Exception:
        pass

def stop_process(proc, sig=signal.SIGINT):
    """Ask a running ffmpeg / pphap to stop. macOS: SIGINT (ffmpeg / pphap finish the file cleanly).
    Windows has no signals for windowless child processes, so it is ended there."""
    try:
        if IS_WIN:
            proc.terminate()
        else:
            proc.send_signal(sig)
    except Exception:
        pass

def filter_path(path: str) -> str:
    """A file path for use inside an ffmpeg filter option (forward slashes, ':' escaped)."""
    return path.replace('\\', '/').replace(':', '\\:').replace("'", "\\'")
VIDEO_EXTS = {'.mov', '.mp4', '.avi', '.mkv', '.mxf', '.m4v', '.ts', '.webm', '.flv', '.wmv'}
IMAGE_EXTS = {'.png', '.tif', '.tiff', '.jpg', '.jpeg', '.exr', '.dpx', '.tga', '.bmp'}

# ─── Image sequences ──────────────────────────────────────────────────────────
# A sequence is queued as an ffmpeg pattern path (…/shot_%04d.png); its first
# frame number, frame count and frame rate are kept here, keyed by that path.
SEQUENCES: Dict[str, dict] = {}
_SEQ_RE = re.compile(r'^(.*?)(\d+)(\.[A-Za-z0-9]+)$')
SEQ_RATE_ALIASES = {'23.976': '24000/1001', '23.98': '24000/1001', '29.97': '30000/1001',
                    '47.952': '48000/1001', '59.94': '60000/1001', '119.88': '120000/1001'}

def _seq_runs(folder: Path, pre: str, width: int, ext: str, names: list) -> Optional[Tuple[str, int, int]]:
    rx = re.compile(re.escape(pre) + r'(\d{%d})' % width + re.escape(ext) + '$')
    nums = sorted({int(m.group(1)) for n in names if (m := rx.match(n))})
    if len(nums) < 2:
        return None
    have = set(nums); start = nums[0]; count = 1
    while start + count in have:          # ffmpeg stops at the first missing frame
        count += 1
    return str(folder / f'{pre}%0{width}d{ext}'), start, count

def detect_sequence(path: str) -> Optional[Tuple[str, int, int]]:
    """(pattern, first frame, frame count) if `path` is one image of a numbered sequence."""
    p = Path(path)
    m = _SEQ_RE.match(p.name)
    if not m or p.suffix.lower() not in IMAGE_EXTS:
        return None
    pre, digits, ext = m.groups()
    try:
        names = os.listdir(p.parent)
    except OSError:
        return None
    return _seq_runs(p.parent, pre, len(digits), ext, names)

def find_sequences(folder) -> list:
    """All numbered image sequences directly inside `folder`."""
    folder = Path(folder)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    keys = []
    for n in names:
        m = _SEQ_RE.match(n)
        if m and Path(n).suffix.lower() in IMAGE_EXTS:
            k = (m.group(1), len(m.group(2)), m.group(3))
            if k not in keys:
                keys.append(k)
    return [r for k in keys if (r := _seq_runs(folder, k[0], k[1], k[2], names))]

def parse_rate(s: str) -> Optional[str]:
    s = (s or '').strip()
    if s in SEQ_RATE_ALIASES:
        return SEQ_RATE_ALIASES[s]
    try:
        if '/' in s:
            n, d = s.split('/', 1)
            return s if float(n) > 0 and float(d) > 0 else None
        return f'{float(s):g}' if float(s) > 0 else None
    except ValueError:
        return None

def rate_value(r: str) -> float:
    try:
        if '/' in r:
            n, d = r.split('/', 1); return float(n) / float(d)
        return float(r)
    except (ValueError, ZeroDivisionError):
        return 0.0

def register_sequence(pattern: str, start: int, count: int, rate: str):
    SEQUENCES[pattern] = {'start': start, 'count': count, 'rate': rate}

def seq_input_args(path: str) -> list:
    s = SEQUENCES.get(path)
    return ['-framerate', s['rate'], '-start_number', str(s['start'])] if s else []

def seq_label(path: str) -> str:
    s = SEQUENCES.get(path)
    if not s:
        return Path(path).name
    m = re.match(r'^(.*)%0(\d+)d(.*)$', Path(path).name)
    if not m:
        return Path(path).name
    w = int(m.group(2))
    return f"{m.group(1)}[{s['start']:0{w}d}-{s['start'] + s['count'] - 1:0{w}d}]{m.group(3)}"

ACCENT       = '#7B6CFF'
ACCENT_HOVER = '#6A5CEE'
GREEN        = '#3DFFA0'
AMBER        = '#FFBE3D'
RED          = '#FF5A5A'
BG           = '#0A0A0F'
SURFACE      = '#12121A'
SURFACE2     = '#1A1A24'
BORDER       = '#2A2A38'
TEXT         = '#E8E8F0'
TEXT2        = '#8888AA'

HAP_VARIANTS = {
    'Hap':       {'flag': 'hap',       'tex': 'DXT1',  'info': 'Smallest files. No alpha. GPU-decoded.'},
    'Hap Alpha': {'flag': 'hap_alpha', 'tex': 'DXT5',  'info': 'DXT5 with full alpha channel.'},
    'Hap Q':     {'flag': 'hap_q',     'tex': 'YCoCg', 'info': 'Best quality. Larger files.'},
    'Hap R':     {'flag': None,        'tex': 'BC7',   'info': 'Highest quality + alpha.'},
}

VARIANT_SUFFIXES = {
    'Hap':       '_Hap',
    'Hap Alpha': '_HapA',
    'Hap Q':     '_HapQ',
    'Hap R':     '_HapR',
}

AUDIO_MODES = {
    'Copy (passthrough)': 'copy',
    'PCM 16-bit':         'pcm_s16le',
    'No audio':           None,
}

FRAME_RATES = ['Original', '23.976', '24', '25', '29.97', '30', '50', '59.94', '60']
RESOLUTIONS = ['Original', '3840x2160', '2048x2048', '1920x1080', '1280x720', 'Custom…']
THREAD_OPTS = ['1 (sequential)', '2', '3', '4', '6', '8']
# texture chunks per Hap frame (players unpack chunks in parallel); Auto = 8
CHUNK_OPTS = ['Auto (8)', '1', '2', '4', '8', '12', '16', '24', '32', '48', '64']
# 1.5.24: settings that belong to each video in the queue (the rest are app-wide)
PER_JOB_KEYS = ('variant', 'audio', 'fps', 'resolution', 'custom_res', 'premultiply', 'tc_burnin',
                'lut', 'prefix', 'suffix', 'extra', 'out_dir', 'chunks')

def chunks_from_label(label: str) -> int:
    try:
        return int(label)
    except (TypeError, ValueError):
        return 0            # Auto

ST_QUEUED    = 'queued'
ST_ENCODING  = 'encoding'
ST_DONE      = 'done'
ST_ERROR     = 'error'
ST_CANCELLED = 'cancelled'

# Keyboard shortcuts all use Cmd (Mac) / Ctrl (Windows): bare keys such as Space would also fire
# while typing in a text field (Tk delivers window-level keys to every widget in the window).
SHORTCUTS = {
    f'<{MOD_KEY}-o>':      '_browse_files',
    f'<{MOD_KEY}-Return>': '_start_shortcut',
    f'<{MOD_KEY}-KP_Enter>': '_start_shortcut',
    f'<{MOD_KEY}-k>':      '_cancel_all',
    f'<{MOD_KEY}-r>':      '_clear_done',
    '<Escape>':            '_clear_selection',
}


# ─── Tool finders ─────────────────────────────────────────────────────────────

def _which(name: str) -> Optional[str]:
    f = shutil.which(name)
    if f:
        return f
    for d in BREW_PATHS:
        c = os.path.join(d, name if name.endswith(EXE) else name + EXE)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None

def _bundled(name: str) -> Optional[str]:
    """ffmpeg/ffprobe shipped inside the app (or ../3rd Party when run from source)."""
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent / '3rd Party'
    c = base / (name + EXE)
    if IS_WIN and not getattr(sys, 'frozen', False) and not c.is_file():
        c = base / 'Windows' / (name + EXE)      # 3rd Party/Windows/ffmpeg.exe when run from source
    if c.is_file() and os.access(str(c), os.X_OK):
        return str(c)
    return None

def find_ffmpeg() -> Optional[str]:
    return _bundled('ffmpeg') or _which('ffmpeg') or _which('ffmpeg.exe')

def find_ffprobe() -> Optional[str]:
    return _bundled('ffprobe') or _which('ffprobe') or _which('ffprobe.exe')

def find_licenses_file() -> Optional[str]:
    """THIRD-PARTY-LICENSES.txt: inside the app, or Source Code/licenses when run from source."""
    cands = []
    if getattr(sys, 'frozen', False):
        cands.append(Path(sys._MEIPASS) / 'THIRD-PARTY-LICENSES.txt')
    cands.append(Path(__file__).resolve().parent / 'licenses' / 'THIRD-PARTY-LICENSES.txt')
    return next((str(c) for c in cands if c.is_file()), None)

BURNIN_FONTS = ['/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/SFNSMono.ttf',
                '/System/Library/Fonts/Helvetica.ttc', 'C:/Windows/Fonts/consola.ttf',
                'C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf']

def burnin_font() -> Optional[str]:
    """A font file for drawtext (the bundled ffmpeg has no fontconfig)."""
    return next((f for f in BURNIN_FONTS if os.path.isfile(f)), None)

def find_pphap() -> Optional[str]:
    """The PixelPress HAP plugin's encoder CLI: bundled in the app (or ../3rd Party), else installed."""
    return _bundled('pphap') or _which('pphap')

def pphap_env() -> dict:
    """Environment for running pphap: points it at our ffmpeg/ffprobe, drops PyInstaller vars."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('DYLD_') and k not in ('_MEIPASS', '_MEIPASS2')}
    for var, tool in (('PPHAP_FFMPEG', find_ffmpeg()), ('PPHAP_FFPROBE', find_ffprobe())):
        if tool:
            env[var] = tool
    return env

PPHAP_HAPR_QUALITY = 'fast'   # Fast keeps opaque alpha at exactly 255 and matches the plugin default

_PPHAP_VERSIONS: dict = {}

def pphap_version(pphap: Optional[str] = None) -> tuple:
    """(major, minor, patch) of pphap, () if unknown."""
    pphap = pphap or find_pphap()
    if not pphap:
        return ()
    if pphap not in _PPHAP_VERSIONS:
        v = ()
        try:
            r = subprocess.run([pphap, '-version'], capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=15, env=pphap_env())
            m = re.search(r'(\d+)\.(\d+)\.(\d+)', r.stdout or '')
            if m:
                v = tuple(int(x) for x in m.groups())
        except Exception:
            pass
        _PPHAP_VERSIONS[pphap] = v
    return _PPHAP_VERSIONS[pphap]

def find_hapr_decoder() -> Optional[str]:
    """pphap 1.1.7+ reads and decodes Hap R (ffmpeg can't; 1.1.6 was internal). None if not available."""
    p = find_pphap()
    return p if p and pphap_version(p) >= (1, 1, 7) else None

def start_hapr_decode(path: str, extra: Optional[list] = None):
    """Start `pphap -i path [extra] -f rawvideo -pix_fmt rgba -` and read its header line.
    Returns (process, width, height, frame_count); frames are RGBA on process.stdout."""
    pphap = find_hapr_decoder()
    if not pphap:
        raise RuntimeError('Hap R decoding needs pphap 1.1.7 or later')
    # pphap runs in the temp folder, so the input path is made absolute
    cmd = [pphap, '-nostats', '-i', os.path.abspath(path)] + (extra or []) + ['-f', 'rawvideo', '-pix_fmt', 'rgba', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=pphap_env(), cwd=tempfile.gettempdir())
    got: list = []
    reader = threading.Thread(target=lambda: got.append(proc.stderr.readline()), daemon=True)
    reader.start()
    reader.join(None if not IS_WIN else 5)
    if reader.is_alive():
        # pphap 1.1.8 on Windows buffers its messages when they go to a pipe, so the header line only
        # arrives when it exits - and it can't exit while nobody reads its frames. Get the frame size
        # from a one-frame run instead, then start the real decode (frame count unknown: -1).
        proc.kill(); proc.wait(); reader.join(5)
        probe = subprocess.run(cmd[:-5] + ['-frames:v', '1'] + cmd[-5:], capture_output=True,
                               env=pphap_env(), cwd=tempfile.gettempdir())
        m = re.search(r' (\d+)x(\d+) @ ', probe.stderr.decode(errors='replace'))
        if not m or probe.returncode != 0:
            msg = probe.stderr.decode(errors='replace').strip().splitlines()
            raise RuntimeError(msg[-1] if msg else f'pphap exit {probe.returncode}')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=pphap_env(), cwd=tempfile.gettempdir())
        return proc, int(m.group(1)), int(m.group(2)), -1
    first = (got[0] if got else b'').decode(errors='replace')
    m = re.search(r' (\d+)x(\d+) @ ', first)
    if not m:
        rest = proc.stderr.read().decode(errors='replace')
        proc.wait()
        msg = (first + rest).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f'pphap exit {proc.returncode}')
    fm = re.search(r', (\d+) frames', first)
    return proc, int(m.group(1)), int(m.group(2)), int(fm.group(1)) if fm else -1

def find_hapr_encoder() -> Optional[str]:
    """Hap R encoder: pphap (same encoder as the plugin) if available, else the legacy hapr_enc."""
    return find_pphap() or find_legacy_hapr()

def find_legacy_hapr() -> Optional[str]:
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent / 'Deployment'
    c = base / 'hapr_enc'
    if c.is_file() and os.access(str(c), os.X_OK):
        return str(c)
    return None


# ─── Formatters ───────────────────────────────────────────────────────────────

def format_bytes(n: int) -> str:
    if not n:
        return ''
    if n < 1024:
        return f'{n} B'
    if n < 1_048_576:
        return f'{n/1024:.1f} KB'
    if n < 1_073_741_824:
        return f'{n/1_048_576:.1f} MB'
    return f'{n/1_073_741_824:.2f} GB'

def format_time(secs) -> str:
    secs = int(secs or 0)
    if secs < 60:
        return f'{secs}s'
    m, s = divmod(secs, 60)
    if m < 60:
        return f'{m}m {s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h {m:02d}m {s:02d}s'

def probe_info(path: str) -> dict:
    ffp = find_ffprobe()
    if not ffp:
        return {}
    try:
        r = subprocess.run(
            [ffp, '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format',
             '-show_entries', 'stream=codec_type,width,height,r_frame_rate,codec_name,codec_tag_string,pix_fmt',
             '-show_entries', 'format=duration,size,bit_rate'] + seq_input_args(path) + [path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30
        )
        if r.returncode != 0:
            return {}
        raw = json.loads(r.stdout)
        streams = raw.get('streams', [])
        fmt     = raw.get('format', {})
        vs = next((s for s in streams if s.get('codec_type') == 'video'), streams[0] if streams else {})
        rf = vs.get('r_frame_rate', '0/1')
        try:
            num, den = rf.split('/')
            fps = round(int(num) / int(den), 3) if int(den) else 0
        except Exception:
            fps = 0
        seq = SEQUENCES.get(path)
        if seq:
            fps = round(rate_value(seq['rate']), 3)
            fmt['duration'] = seq['count'] / rate_value(seq['rate'])
            fmt['size'] = 0
        return {
            'width':    vs.get('width', 0),
            'height':   vs.get('height', 0),
            'fps':      fps,
            'rate':     rf,
            'codec':    vs.get('codec_name', 'none'),
            'codec_tag': vs.get('codec_tag_string', ''),
            'pix_fmt':  vs.get('pix_fmt', '?'),
            'duration': float(fmt.get('duration') or 0),
            'size':     int(fmt.get('size') or 0),
            'bitrate':  int(fmt.get('bit_rate') or 0),
        }
    except Exception:
        return {}

def unsupported_source_reason(info: dict):
    """Return (short_status, long_detail) if ffmpeg can't decode this source, else None."""
    if not info:
        return None
    tag   = (info.get('codec_tag') or '').strip()
    codec = (info.get('codec') or '').strip().lower()
    if tag == 'Hap7':
        if find_hapr_decoder():
            return None         # pphap 1.1.7+ decodes Hap R itself
        return ("Source is Hap R \u2014 can't be read",
                "This file is already encoded as Hap R (BC7). ffmpeg has no Hap R "
                "decoder; reading Hap R needs pphap 1.1.7 or later (PixelPress HAP "
                "1.1.7), which this copy of PixelPress doesn't include.\n\n"
                "Encode from the original master instead (ProRes, H.264, PNG "
                "sequence, etc.).")
    if codec in ('none', '', '?') and info.get('width'):
        return ('Unsupported source codec',
                f"ffmpeg has no decoder for this video stream "
                f"(codec tag: {tag or 'unknown'}). Re-export the source to "
                f"ProRes or H.264 and try again.")
    return None

def probe_duration(path: str) -> Optional[float]:
    info = probe_info(path)
    try:
        return float(info['duration'])
    except (KeyError, TypeError, ValueError):
        return None

def notify(title: str, message: str):
    """A desktop notification (Notification Centre on macOS, a notification balloon on Windows)."""
    try:
        if IS_MAC:
            subprocess.Popen(['osascript', '-e',
                              f'display notification "{message}" with title "{title}"'])
        elif IS_WIN:
            t = title.replace("'", "''"); m = message.replace("'", "''")
            ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                  "$n = New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                  f"$n.ShowBalloonTip(8000, '{t}', '{m}', 'Info'); Start-Sleep -Seconds 9; $n.Dispose()")
            subprocess.Popen(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps])
    except Exception:
        pass

notify_macos = notify   # old name

def build_vf_chain(resolution: str, fps: str, premultiply: bool,
                   lut_path: str, tc_burnin: bool, extra_flags: str) -> list:
    filters = []
    if premultiply:
        filters.append('premultiply=inplace=1')
    if lut_path and os.path.isfile(lut_path):
        filters.append(f"lut3d='{filter_path(lut_path)}'")
    if tc_burnin:
        font = burnin_font()
        fontopt = f"fontfile='{filter_path(font)}':" if font else ''
        filters.append(
            f"drawtext={fontopt}text='%{{pts\\:hms}}':fontsize=24:fontcolor=white"
            ":box=1:boxcolor=black@0.5:x=10:y=10"
        )
    if resolution and resolution not in ('Original', ''):
        if 'x' in resolution:
            w, h = resolution.split('x')
            filters.append(f'scale={w}:{h}')
    filters.append('pad=ceil(iw/4)*4:ceil(ih/4)*4')

    args: list = []
    if fps and fps not in ('Original', ''):
        args += ['-r', fps]

    extra_vf = ''
    extra_other: list = []
    if extra_flags:
        parts = shlex.split(extra_flags)
        i = 0
        while i < len(parts):
            if parts[i] == '-vf' and i + 1 < len(parts):
                extra_vf = parts[i + 1]
                i += 2
            else:
                extra_other.append(parts[i])
                i += 1

    if extra_vf:
        filters.append(extra_vf)
    if filters:
        args += ['-vf', ','.join(filters)]
    args.extend(extra_other)
    return args

def _job_filters(job) -> str:
    """The job's video options as one ffmpeg filter string (for pphap / Hap R): same filters as
    build_vf_chain, plus fps= for a frame-rate change (ffmpeg's -r, which pphap doesn't resample).
    Empty when the job changes nothing."""
    args = build_vf_chain(job.resolution, 'Original', job.premultiply, job.lut_path,
                          job.tc_burnin, job.extra_flags)
    vf = args[args.index('-vf') + 1] if '-vf' in args else ''
    parts = [p for p in vf.split(',') if p] if vf else []
    if parts and parts[-1] == 'pad=ceil(iw/4)*4:ceil(ih/4)*4':
        parts = parts[:-1]                  # pphap pads to whole 4x4 blocks itself
    if job.fps and job.fps not in ('Original', ''):
        parts.append(f"fps={SEQ_RATE_ALIASES.get(job.fps, job.fps)}")
    if not parts:
        return ''
    return ','.join(parts + ['pad=ceil(iw/4)*4:ceil(ih/4)*4'])


def build_ffmpeg_args(inp: str, out: str, flag: str, audio: Optional[str],
                      resolution: str, fps: str, premultiply: bool,
                      lut_path: str, tc_burnin: bool, extra_flags: str, chunks: int = 0) -> list:
    vf_args = build_vf_chain(resolution, fps, premultiply, lut_path, tc_burnin, extra_flags)
    args = seq_input_args(inp) + ['-i', inp] + vf_args + ['-vcodec', 'hap', '-format', flag]
    if chunks > 0:
        args += ['-chunks', str(chunks)]
    if audio is None:
        args += ['-an']
    else:
        args += ['-acodec', audio]
    args.append(out)
    return args

def make_output_path(inp: str, out_dir: str, prefix: str,
                     suffix: str, variant_name: str) -> str:
    """<prefix><name><optional custom suffix><codec tag>.mov  e.g. clip_v2_HapQ.mov"""
    p    = Path(inp)
    pstem = re.sub(r'[._ -]*%0\d+d$', '', p.stem) if '%0' in p.stem else p.stem
    sfx  = suffix.strip()
    vsfx = VARIANT_SUFFIXES.get(variant_name, '')
    # don't double up if the custom suffix already ends with the codec tag
    if vsfx and sfx.lower().endswith(vsfx.lower()):
        vsfx = ''
    # strip a stale codec tag left in the custom suffix from another variant
    for other in VARIANT_SUFFIXES.values():
        if vsfx and sfx.endswith(other):
            sfx = sfx[:-len(other)]
            break
    stem = f'{prefix.strip()}{pstem}{sfx}{vsfx}'
    base = Path(out_dir.strip()).expanduser() if out_dir and out_dir.strip() else p.parent
    return str(base / f'{stem}.mov')


# ─── Persistence ──────────────────────────────────────────────────────────────

def load_presets() -> dict:
    if PRESETS_FILE.exists():
        try:
            return json.loads(PRESETS_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}

def save_presets(presets: dict):
    PRESETS_FILE.write_text(json.dumps(presets, indent=2), encoding='utf-8')

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}

def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding='utf-8')

def sanitize_filename(name: str) -> str:
    return re.sub(r'[^\w\-_. ]', '_', name).strip()


# ─── EncodeJob ────────────────────────────────────────────────────────────────

class EncodeJob:
    _id = 0

    def __init__(self, *, input_path: str, output_path: str, variant_flag: str,
                 audio_codec: Optional[str], resolution: str, fps: str,
                 premultiply: bool = False, lut_path: str = '',
                 tc_burnin: bool = False, extra_flags: str = '',
                 rename_source: bool = False,
                 out_dir: str = '', prefix: str = '', suffix: str = '',
                 variant_name: str = '', chunks: int = 0):
        EncodeJob._id += 1
        self.job_id       = EncodeJob._id
        self.uid          = str(uuid.uuid4())[:8]
        self.input_path   = input_path
        self.output_path  = output_path
        self.variant_flag = variant_flag
        self.out_dir      = out_dir
        self.prefix       = prefix
        self.suffix       = suffix
        self.variant_name = variant_name
        self.audio_codec  = audio_codec
        self.resolution   = resolution
        self.fps          = fps
        self.premultiply  = premultiply
        self.lut_path     = lut_path
        self.tc_burnin    = tc_burnin
        self.extra_flags  = extra_flags
        self.rename_source = rename_source
        self.chunks       = chunks          # 0 = encoder default (8)
        self.settings: dict = {}            # 1.5.24: this video's own panel settings (PER_JOB_KEYS)
        self.customized   = False           # edited on its own (selected in the queue)
        self.state        = ST_QUEUED
        self.progress     = 0.0
        self.status_text  = 'Queued'
        self.error_detail = ''
        self.full_stderr  = []
        self.cmd_str      = ''
        self.duration     = None
        self.output_size  = None
        self.encode_time  = None
        self.input_info   = {}
        self.row_widget   = None
        self.hapr_bin     = None
        self.custom_output = False   # True once the user picks Save As… for this job


# ─── JobRow ───────────────────────────────────────────────────────────────────

class JobRow(ctk.CTkFrame):
    def __init__(self, parent, job: EncodeJob, on_remove, on_move_up, on_move_down, on_preview,
                 on_save_as=None, on_select=None, summary=None):
        super().__init__(parent, fg_color=SURFACE2, corner_radius=8,
                         border_width=1, border_color=BORDER)
        self.job = job
        self._on_select = on_select
        self._summary_fn = summary
        self.grid_columnconfigure(1, weight=1)

        # Left control strip (up/dot/down)
        ctrl = ctk.CTkFrame(self, fg_color='transparent', width=28)
        ctrl.grid(row=0, column=0, padx=(6, 0), pady=6, sticky='ns')

        ctk.CTkButton(ctrl, text='↑', width=22, height=20,
                      fg_color='transparent', hover_color=SURFACE,
                      text_color=TEXT2, font=('', 11),
                      command=lambda j=job, cb=on_move_up: cb(j)).pack()

        self._dot = ctk.CTkLabel(ctrl, text='●', font=('', 12),
                                 text_color=TEXT2, width=22)
        self._dot.pack(pady=2)

        ctk.CTkButton(ctrl, text='↓', width=22, height=20,
                      fg_color='transparent', hover_color=SURFACE,
                      text_color=TEXT2, font=('', 11),
                      command=lambda j=job, cb=on_move_down: cb(j)).pack()

        # Main info area
        info = ctk.CTkFrame(self, fg_color='transparent')
        info.grid(row=0, column=1, sticky='ew', pady=(8, 0))
        info.grid_columnconfigure(0, weight=1)

        # 1.5.24: click selects the video (its settings show in the panel); double-click previews
        self._name_btn = ctk.CTkLabel(
            info, text=seq_label(job.input_path),
            font=('', 12, 'bold'), text_color=TEXT, anchor='w', cursor='hand2')
        self._name_btn.grid(row=0, column=0, sticky='w', padx=(4, 4))
        self._name_btn.bind('<Double-Button-1>', lambda e, j=job, cb=on_preview: cb(j))
        self._summary_lbl = ctk.CTkLabel(info, text='', font=('', 10), text_color=ACCENT,
                                         anchor='w', justify='left', wraplength=560)
        self._summary_lbl.grid(row=3, column=0, sticky='w', padx=(4, 0), pady=(0, 6))

        self._out_lbl = ctk.CTkLabel(
            info, text='', justify='left',
            font=('', 10), text_color=TEXT2, anchor='w', wraplength=300)
        self._out_lbl.grid(row=1, column=0, sticky='w', pady=(1, 0))
        self._save_as_btn = None
        if on_save_as:
            self._save_as_btn = ctk.CTkButton(
                info, text='Save As…', width=72, height=22,
                fg_color='transparent', hover_color=SURFACE,
                text_color=TEXT2, font=('', 10),
                border_width=1, border_color=BORDER,
                command=lambda j=job, cb=on_save_as: cb(j))
            self._save_as_btn.grid(row=1, column=1, sticky='e', padx=(4, 0))
        self.refresh_output()

        self._status_lbl = ctk.CTkLabel(
            info, text='Queued', font=('Courier', 10),
            text_color=TEXT2, anchor='w', wraplength=280)
        self._status_lbl.grid(row=2, column=0, sticky='w', pady=(2, 0))
        self.refresh_summary()
        for w in (self, info, self._name_btn, self._out_lbl, self._status_lbl, self._summary_lbl):
            w.bind('<Button-1>', self._clicked, add='+')

        # Progress bar
        self._bar = ctk.CTkProgressBar(self, height=4, fg_color=BORDER,
                                       progress_color=ACCENT, corner_radius=2)
        self._bar.set(0)

        # Remove button
        self._rm_btn = ctk.CTkButton(
            self, text='✕', width=28, height=28,
            fg_color='transparent', hover_color=SURFACE,
            text_color=TEXT2, font=('', 13),
            command=lambda j=job, cb=on_remove: cb(j))
        self._rm_btn.grid(row=0, column=2, padx=(4, 10), sticky='n', pady=(8, 0))

    def _clicked(self, event=None):
        if self._on_select:
            self._on_select(self.job, event)

    def set_selected(self, on: bool):
        self.configure(border_width=2 if on else 1, border_color=ACCENT if on else BORDER)

    def refresh_summary(self):
        if self._summary_fn and hasattr(self, '_summary_lbl'):
            self._summary_lbl.configure(text=self._summary_fn(self.job))

    def refresh_output(self):
        op = Path(self.job.output_path)
        folder = str(op.parent)
        home = str(Path.home())
        if folder.startswith(home):
            folder = '~' + folder[len(home):]
        self._out_lbl.configure(text=f'→ {op.name}\n   in {folder}')

    def _show_details(self):
        job = self.job
        if job.full_stderr:
            lines = job.full_stderr[-20:]
        else:
            lines = [job.error_detail or 'No details']
        text = '\n'.join(lines)
        if job.cmd_str:
            text = f'Command:\n{job.cmd_str}\n\n---\n\n{text}'
        messagebox.showinfo('Error Details', text)

    def update_state(self):
        job = self.job
        s   = job.state
        if s == ST_QUEUED:
            tc, nc = TEXT2, TEXT2
        elif s == ST_ENCODING:
            tc, nc = ACCENT, ACCENT
        elif s == ST_DONE:
            tc, nc = GREEN, GREEN
        elif s == ST_ERROR:
            tc, nc = RED, RED
        elif s == ST_CANCELLED:
            tc, nc = AMBER, AMBER
        else:
            tc, nc = TEXT2, TEXT2

        self._dot.configure(text_color=nc)
        if self._save_as_btn is not None:
            if s == ST_QUEUED:
                self._save_as_btn.grid()
            else:
                self._save_as_btn.grid_remove()
        txt = job.get('status_text', '') if isinstance(job, dict) else job.status_text
        self._status_lbl.configure(text=txt or s, text_color=tc)
        self._bar.set(job.progress)

        if s in (ST_DONE, ST_ERROR):
            self._bar.grid(row=1, column=0, columnspan=2, padx=(8, 0), pady=(0, 6), sticky='ew')
        else:
            self._bar.grid(row=1, column=0, columnspan=2, padx=(8, 0), pady=(0, 6), sticky='ew')

        if s == ST_ERROR:
            if not hasattr(self, '_detail_btn'):
                self._detail_btn = ctk.CTkButton(
                    self, text='Details', width=60, height=22,
                    fg_color='transparent', hover_color=SURFACE,
                    text_color=TEXT2, font=('', 10),
                    border_width=1, border_color=BORDER,
                    command=self._show_details)
                self._detail_btn.grid(row=2, column=0, columnspan=2,
                                      padx=(0, 8), pady=(0, 6), sticky='e')
        elif hasattr(self, '_detail_btn'):
            self._detail_btn.grid_remove()


# ─── FilePreviewDialog ────────────────────────────────────────────────────────

class FilePreviewDialog(ctk.CTkToplevel):
    def __init__(self, parent, job: EncodeJob):
        super().__init__(parent)
        self.title('File Info')
        self.geometry('420x320')
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()

        ctk.CTkLabel(self, text=seq_label(job.input_path),
                     font=('', 14, 'bold'), text_color=TEXT,
                     wraplength=380).pack(padx=20, pady=(20, 4), anchor='w')

        card = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=10,
                            border_width=1, border_color=BORDER)
        card.pack(padx=20, pady=8, fill='both', expand=True)

        info = job.input_info
        if not info:
            info = probe_info(job.input_path)
            job.input_info = info

        bitrate_val = (f"{int(info.get('bitrate', 0) / 1000)} kb/s"
                       if info.get('bitrate') else '?')
        rows = [
            ('Resolution',   f"{info.get('width', 0)} × {info.get('height', 0)} px"),
            ('Frame rate',   f"{info.get('fps', 0)} fps"),
            ('Duration',     format_time(info.get('duration', 0))),
            ('File size',    format_bytes(info.get('size', 0))),
            ('Codec',        info.get('codec', '?')),
            ('Pixel format', info.get('pix_fmt', '?')),
            ('Bitrate',      bitrate_val),
            ('Output',       Path(job.output_path).name),
        ]
        for i, (label, val) in enumerate(rows):
            r = ctk.CTkFrame(card, fg_color='transparent')
            r.pack(fill='x', padx=16, pady=(6 if i == 0 else 2, 2))
            ctk.CTkLabel(r, text=label, font=('', 11), text_color=TEXT2,
                         width=110, anchor='w').pack(side='left')
            ctk.CTkLabel(r, text=val, font=('Courier', 11),
                         text_color=TEXT, anchor='w').pack(side='left')

        ctk.CTkButton(self, text='Close', height=34, fg_color=SURFACE2,
                      hover_color=BORDER, text_color=TEXT2, font=('', 12),
                      border_width=1, border_color=BORDER,
                      command=self.destroy).pack(padx=20, pady=12)


# ─── BatchRenameDialog ────────────────────────────────────────────────────────

class BatchRenameDialog(ctk.CTkToplevel):
    def __init__(self, parent, jobs: list, on_apply):
        super().__init__(parent)
        self.title('Batch Rename Sources')
        self.geometry('420x260')
        self.configure(fg_color=BG)
        self.grab_set()
        self.jobs = jobs
        self.on_apply = on_apply

        ctk.CTkLabel(self, text='Rename source files', font=('', 13, 'bold'),
                     text_color=TEXT).pack(padx=20, pady=(16, 4), anchor='w')
        ctk.CTkLabel(self, text='Changes take effect immediately on disk.',
                     font=('', 11), text_color=TEXT2).pack(padx=20, anchor='w')

        card = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=10,
                            border_width=1, border_color=BORDER)
        card.pack(padx=20, pady=10, fill='x')
        card.grid_columnconfigure(1, weight=1)

        self._find_var = tk.StringVar()
        self._repl_var = tk.StringVar()
        self._pre_var  = tk.StringVar()
        self._suf_var  = tk.StringVar()
        self._sanitize_var = tk.BooleanVar(value=False)

        rows = [
            ('Find',    self._find_var),
            ('Replace', self._repl_var),
            ('Prefix',  self._pre_var),
            ('Suffix',  self._suf_var),
        ]
        for i, (lbl, var) in enumerate(rows):
            ctk.CTkLabel(card, text=lbl, font=('', 11), text_color=TEXT2,
                         anchor='w').grid(row=i, column=0, padx=(12, 4), pady=3, sticky='w')
            ctk.CTkEntry(card, textvariable=var, height=28, font=('', 11),
                         fg_color=SURFACE2, border_color=BORDER,
                         text_color=TEXT).grid(row=i, column=1, padx=(0, 12), pady=3, sticky='ew')

        ctk.CTkCheckBox(card, text='Sanitize filenames', variable=self._sanitize_var,
                        font=('', 11), text_color=TEXT2,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER
                        ).grid(row=len(rows), column=0, columnspan=2,
                               padx=12, pady=(4, 8), sticky='w')

        r = ctk.CTkFrame(self, fg_color='transparent')
        r.pack(padx=20, pady=(0, 16), fill='x')
        ctk.CTkButton(r, text='Cancel', width=100, height=34,
                      fg_color=SURFACE, hover_color=SURFACE2,
                      text_color=TEXT2, border_width=1, border_color=BORDER,
                      command=self.destroy).pack(side='left')
        ctk.CTkButton(r, text='Apply', width=100, height=34,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color='white',
                      command=self._apply).pack(side='right')

    def _apply(self):
        find     = self._find_var.get()
        repl     = self._repl_var.get()
        pre      = self._pre_var.get()
        suf      = self._suf_var.get()
        sanitize = self._sanitize_var.get()

        for i, job in enumerate(self.jobs):
            p    = Path(job.input_path)
            stem = p.stem
            if find:
                stem = stem.replace(find, repl)
            stem = f'{pre}{stem}{suf}'
            if sanitize:
                stem = sanitize_filename(stem)
            new_path = p.parent / f'{stem}{p.suffix}'
            try:
                p.rename(new_path)
                job.input_path = str(new_path)
                if job.row_widget:
                    job.row_widget._name_btn.configure(text=new_path.name)
            except Exception:
                pass

        self.on_apply()
        self.destroy()


# ─── PresetDialog ─────────────────────────────────────────────────────────────

class PresetDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_save, settings: dict):
        super().__init__(parent)
        self.title('Save Preset')
        self.geometry('340x160')
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()

        ctk.CTkLabel(self, text='Preset name', font=('', 12),
                     text_color=TEXT).pack(padx=24, pady=(20, 4), anchor='w')
        self._e = ctk.CTkEntry(self, height=36, font=('', 12),
                               fg_color=SURFACE2, border_color=BORDER,
                               text_color=TEXT)
        self._e.pack(padx=24, fill='x')
        self._e.focus()

        r = ctk.CTkFrame(self, fg_color='transparent')
        r.pack(padx=24, pady=16, fill='x')
        ctk.CTkButton(r, text='Cancel', width=100, height=34,
                      fg_color=SURFACE, hover_color=SURFACE2,
                      text_color=TEXT2, border_width=1, border_color=BORDER,
                      command=self.destroy).pack(side='left')
        ctk.CTkButton(r, text='Save', width=100, height=34,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color='white',
                      command=lambda: self._save(on_save, settings)).pack(side='right')

    def _save(self, on_save, settings: dict):
        n = self._e.get().strip()
        if n:
            on_save(n, settings)
            self.destroy()


# ─── _VariantCard ─────────────────────────────────────────────────────────────

class _VariantCard(ctk.CTkFrame):
    def __init__(self, parent, name: str, tex: str, info: str,
                 is_external: bool = False, selected: bool = False, command=None):
        super().__init__(parent,
                         fg_color=SURFACE2 if selected else SURFACE,
                         corner_radius=8,
                         border_width=2 if selected else 1,
                         border_color=ACCENT if selected else BORDER,
                         cursor='hand2')
        self.grid_columnconfigure(0, weight=1)
        self._name_lbl = None
        self._ext_lbl  = None

        self.bind('<Button-1>', lambda e, cb=command: cb() if cb else None)

        nl = ctk.CTkLabel(self, text=name, font=('', 12, 'bold'),
                          text_color=ACCENT if selected else TEXT, anchor='w')
        nl.grid(row=0, column=0, padx=(8, 1), pady=(8, 1), sticky='ew')
        nl.bind('<Button-1>', lambda e, cb=command: cb() if cb else None)
        self._name_lbl = nl

        tl = ctk.CTkLabel(self, text=tex, font=('Courier', 9),
                          text_color=AMBER if is_external else TEXT2,
                          fg_color=SURFACE, corner_radius=3)
        tl.grid(row=1, column=0, padx=(8, 1), pady=(0, 3), sticky='w')
        tl.bind('<Button-1>', lambda e, cb=command: cb() if cb else None)

        il = ctk.CTkLabel(self, text=info, font=('', 9), text_color=TEXT2,
                          wraplength=110, justify='left', anchor='nw')
        il.grid(row=2, column=0, padx=(8, 1), pady=(0, 8), sticky='ew')
        il.bind('<Button-1>', lambda e, cb=command: cb() if cb else None)

        if is_external:
            self._ext_lbl = ctk.CTkLabel(self, text='external tool',
                                         font=('', 8), text_color=AMBER,
                                         fg_color='#2A1F00', corner_radius=3)
            self._ext_lbl.grid(row=3, column=0, padx=(8, 1), pady=(0, 8), sticky='w')
            self._ext_lbl.bind('<Button-1>', lambda e, cb=command: cb() if cb else None)

    def set_selected(self, val: bool):
        self.configure(
            fg_color=SURFACE2 if val else SURFACE,
            border_width=2 if val else 1,
            border_color=ACCENT if val else BORDER
        )
        if self._name_lbl:
            self._name_lbl.configure(text_color=ACCENT if val else TEXT)

    def set_native(self, val: bool):
        lbl = getattr(self, '_ext_lbl', None)
        if lbl is not None:
            if val:
                lbl.grid_remove()
            else:
                lbl.grid()


# ─── PixelPressApp ────────────────────────────────────────────────────────────

_DND_BASES = (TkinterDnD.DnDWrapper,) if TkinterDnD is not None else ()

class PixelPressApp(ctk.CTk, *_DND_BASES):
    _VARIANT_SUFFIXES = VARIANT_SUFFIXES

    def __init__(self):
        super().__init__()
        self.title(f'PixelPress  v{VERSION}')
        self.geometry('960x980')
        self.minsize(800, 640)
        self.configure(fg_color=BG)

        self.ffmpeg_bin  = find_ffmpeg()
        self.hapr_bin    = find_hapr_encoder()

        self._jobs: List[EncodeJob]   = []
        self._encode_queue            = queue.Queue()
        self._running                 = False
        self._active_futures: dict    = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self._current_procs: dict     = {}
        self._watch_active            = False
        self._watch_seen: set         = set()
        self._selected: List[EncodeJob] = []    # 1.5.24: videos whose settings the panel edits

        self._presets       = load_presets()
        self._saved_settings = load_settings()

        self._appearance_mode = tk.StringVar(value='dark')
        ctk.set_appearance_mode('dark')

        self.after(50, self._build_and_show)

    def _build_and_show(self):
        self._build_ui()
        self._restore_settings()
        self._check_ffmpeg()
        self.update_idletasks()
        self.lift()
        self.focus_force()
        self._bind_shortcuts()
        self._setup_drag_drop()
        self._build_menu()

    # ── About / licences ──────────────────────────────────────────────────────

    def _build_menu(self):
        try:
            menubar = tk.Menu(self)
            if sys.platform == 'darwin':
                app_menu = tk.Menu(menubar, name='apple')      # the "PixelPress" menu
                app_menu.add_command(label='About PixelPress', command=self._show_about)
                app_menu.add_command(label='Licences and Source Code…', command=self._show_licenses)
                app_menu.add_separator()
                menubar.add_cascade(menu=app_menu)
            help_menu = tk.Menu(menubar, name='help')
            help_menu.add_command(label='Licences and Source Code…', command=self._show_licenses)
            if sys.platform != 'darwin':
                help_menu.add_command(label='About PixelPress', command=self._show_about)
            menubar.add_cascade(label='Help', menu=help_menu)
            tk.Tk.config(self, menu=menubar)
            try:
                self.createcommand('tkAboutDialog', self._show_about)
            except Exception:
                pass
        except Exception:
            pass

    def _show_about(self):
        pph = find_pphap()
        pv = ''
        if pph:
            try:
                pv = subprocess.run([pph, '-version'], capture_output=True, text=True, encoding='utf-8', errors='replace',
                                    timeout=10, env=pphap_env()).stdout.strip()
            except Exception:
                pass
        fv = ''
        ff = self.ffmpeg_bin or find_ffmpeg()
        if ff:
            try:
                fv = subprocess.run([ff, '-hide_banner', '-version'], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=10).stdout.splitlines()[0]
            except Exception:
                pass
        messagebox.showinfo(
            'About PixelPress',
            f'PixelPress {VERSION}\nHap / Hap Alpha / Hap Q / Hap R encoder\n\n'
            f'Free software by Pixel Mosaic (MIT License).\n'
            f'Hap R: {pv or "pphap not found"}\n{fv}\n\n'
            '"Hap" is the codec by Tom Butterworth / Vidvox. PixelPress is not made or '
            'endorsed by Vidvox or disguise.\n\nHelp › Licences and Source Code… for the '
            'open-source licences.', parent=self)

    def _show_licenses(self):
        path = find_licenses_file()
        win = ctk.CTkToplevel(self)
        win.title('PixelPress — Licences and Source Code')
        win.geometry('760x640')
        win.transient(self)
        box = ctk.CTkTextbox(win, wrap='word', font=(MONO_FONT, 11))
        box.pack(fill='both', expand=True, padx=12, pady=(12, 6))
        try:
            text = Path(path).read_text(encoding='utf-8') if path else 'Licence file not found.'
        except Exception as e:
            text = f'Could not read the licence file: {e}'
        box.insert('1.0', text)
        box.configure(state='disabled')
        bar = ctk.CTkFrame(win, fg_color='transparent')
        bar.pack(fill='x', padx=12, pady=(0, 12))
        if path:
            ctk.CTkButton(bar, text='Show file', width=110,
                          command=lambda: reveal_path(path)).pack(side='left')
        ctk.CTkButton(bar, text='Close', width=90, command=win.destroy).pack(side='right')

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # scrollable: the settings column is taller than most windows
        self._left = ctk.CTkScrollableFrame(self, fg_color='transparent', corner_radius=0,
                                            scrollbar_button_color=BORDER,
                                            scrollbar_button_hover_color=ACCENT)
        self._left.grid(row=0, column=0, sticky='nsew', padx=(0, 0))
        self._left.grid_columnconfigure(0, weight=1)

        right = ctk.CTkFrame(self, fg_color='transparent')
        right.grid(row=0, column=1, sticky='nsew')
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        self._build_left()
        self._build_right(right)
        self.after(0, self._finish_scroll_setup)

    # ── Scrolling ─────────────────────────────────────────────────────────────
    # Tk 9 on macOS sends trackpad scrolling as <TouchpadScroll> (which
    # CustomTkinter doesn't handle) and mouse-wheel steps of ±120, so both are
    # handled here for every scrollable panel.

    def _finish_scroll_setup(self):
        try:
            self.update_idletasks()
            w = self._left.winfo_reqwidth()          # size the column to its content
            if w > 1:
                self._left._parent_canvas.configure(width=w)
        except Exception:
            pass
        self._scroll_frames = [f for f in (getattr(self, '_left', None),
                                           getattr(self, '_job_list_frame', None))
                               if isinstance(f, ctk.CTkScrollableFrame)]
        self.bind_all('<MouseWheel>', self._on_mouse_wheel)      # replaces CTk's handler
        try:
            self.bind_all('<TouchpadScroll>', self._on_touchpad_scroll)
        except tk.TclError:
            pass                                                 # Tk 8.6: no such event

    def _scroll_target(self, event):
        x, y = event.x_root, event.y_root
        for f in getattr(self, '_scroll_frames', []):
            pf = f._parent_frame
            try:
                if not pf.winfo_ismapped():
                    continue
                x0, y0 = pf.winfo_rootx(), pf.winfo_rooty()
                if x0 <= x < x0 + pf.winfo_width() and y0 <= y < y0 + pf.winfo_height():
                    return f
            except tk.TclError:
                pass
        return None

    def _scroll_by_pixels(self, f, px: float):
        c = f._parent_canvas
        top, bot = c.yview()
        if bot - top >= 1.0:
            return                                               # everything already visible
        bbox = c.bbox('all')
        if not bbox or bbox[3] - bbox[1] <= 0:
            return
        c.yview_moveto(min(max(0.0, top + px / (bbox[3] - bbox[1])), 1.0 - (bot - top)))

    def _on_mouse_wheel(self, event):
        f = self._scroll_target(event)
        if f is None:
            return
        d = event.delta
        lines = -d / 120 * 3 if abs(d) >= 120 else -d
        self._scroll_by_pixels(f, lines * 18)

    def _on_touchpad_scroll(self, event):
        f = self._scroll_target(event)
        if f is None:
            return
        try:
            dx, dy = (float(v) for v in self.tk.splitlist(
                self.tk.call('tk::PreciseScrollDeltas', event.delta)))
        except Exception:
            return
        if dy:
            self._scroll_by_pixels(f, -dy)

    # ── Left panel ────────────────────────────────────────────────────────────

    def _build_left(self):
        L = self._left
        row = 0

        # Header row
        hdr = ctk.CTkFrame(L, fg_color='transparent')
        hdr.grid(row=row, column=0, sticky='ew', padx=(24, 0), pady=(24, 0))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text='PixelPress', font=('', 20, 'bold'),
                     text_color=TEXT).grid(row=0, column=0, sticky='w')
        self._ffmpeg_badge = ctk.CTkLabel(hdr, text='', font=('', 10),
                                          text_color=TEXT2, fg_color=SURFACE2,
                                          corner_radius=4)
        self._ffmpeg_badge.grid(row=1, column=0, sticky='w')
        ctk.CTkButton(hdr, text='☀', width=28, height=28,
                      fg_color='transparent', hover_color=SURFACE,
                      text_color=TEXT2, font=('', 14),
                      command=self._toggle_appearance
                      ).grid(row=0, column=1, padx=(0, 8))
        row += 1

        # Add files button
        ctk.CTkButton(L, text='＋  Add Videos / Image Sequences', height=38,
                      font=('', 13), fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color='white', corner_radius=8,
                      border_width=0, border_color=ACCENT,
                      command=self._browse_files
                      ).grid(row=row, column=0, sticky='ew', padx=16, pady=(12, 0))
        row += 1

        # Presets row
        preset_row = ctk.CTkFrame(L, fg_color='transparent')
        preset_row.grid(row=row, column=0, sticky='ew', padx=16, pady=(6, 0))
        preset_row.grid_columnconfigure(0, weight=1)
        self._preset_var = tk.StringVar(value='Presets')
        self._preset_menu = ctk.CTkOptionMenu(
            preset_row, values=self._preset_labels(),
            variable=self._preset_var,
            fg_color=SURFACE, button_color=SURFACE2,
            button_hover_color=BORDER,
            dropdown_fg_color=SURFACE,
            text_color=TEXT2, font=('', 11),
            width=180, command=self._load_preset)
        self._preset_menu.grid(row=0, column=0, sticky='ew')
        ctk.CTkButton(preset_row, text='Save preset', height=30, width=90,
                      fg_color=SURFACE, hover_color=SURFACE2,
                      text_color=TEXT2, font=('', 11),
                      border_width=1, border_color=BORDER,
                      command=self._save_preset_dialog
                      ).grid(row=0, column=1, padx=(4, 0))
        row += 1

        # 1.5.24: which videos the settings below apply to
        self._sel_frame = ctk.CTkFrame(L, fg_color=SURFACE, corner_radius=8,
                                       border_width=1, border_color=BORDER)
        self._sel_frame.grid(row=row, column=0, sticky='ew', padx=16, pady=(8, 0))
        self._sel_frame.grid_columnconfigure(0, weight=1)
        self._sel_label = ctk.CTkLabel(self._sel_frame, text='Settings for newly added videos',
                                       font=('', 11, 'bold'), text_color=TEXT2, anchor='w')
        self._sel_label.grid(row=0, column=0, sticky='w', padx=10, pady=(6, 0))
        ctk.CTkLabel(self._sel_frame, text='Click videos in the queue to give them their own settings',
                     font=('', 10), text_color=TEXT2, anchor='w'
                     ).grid(row=1, column=0, sticky='w', padx=10, pady=(0, 6))
        self._sel_btn = ctk.CTkButton(self._sel_frame, text='Apply to all queued', height=26, width=120,
                                      fg_color=SURFACE2, hover_color=BORDER, text_color=TEXT,
                                      font=('', 11), border_width=1, border_color=BORDER,
                                      command=lambda: self._apply_panel_to_all())
        self._sel_btn.grid(row=0, column=1, rowspan=2, padx=(4, 8), pady=6)
        row += 1

        # ── OUTPUT FOLDER ──
        self._slabel(row, 'OUTPUT FOLDER'); row += 1
        f = self._card(row); row += 1
        f.grid_columnconfigure(0, weight=1)
        self._out_dir_entry = ctk.CTkEntry(
            f, placeholder_text='Same folder as source (default)',
            height=32, font=('', 11),
            fg_color=SURFACE2, border_color=BORDER, text_color=TEXT)
        self._out_dir_entry.grid(row=0, column=0, sticky='ew', padx=(8, 4), pady=8)
        ctk.CTkButton(f, text='Browse', width=64, height=32,
                      fg_color=SURFACE2, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT2, font=('', 11),
                      command=self._browse_out_dir
                      ).grid(row=0, column=1, padx=(0, 4), pady=8)
        ctk.CTkButton(f, text='Use Source', width=76, height=32,
                      fg_color=SURFACE2, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT2, font=('', 11),
                      command=self._reset_out_dir
                      ).grid(row=0, column=2, padx=(0, 8), pady=8)
        self._out_dir_entry.bind('<KeyRelease>', lambda e: self._update_fn_preview())
        self._out_dir_entry.bind('<FocusOut>',   lambda e: self._update_fn_preview())

        after_row = ctk.CTkFrame(f, fg_color='transparent')
        after_row.grid(row=1, column=0, columnspan=3, sticky='ew', padx=8, pady=(0, 8))
        ctk.CTkLabel(after_row, text='After encode:', font=('', 11),
                     text_color=TEXT2).pack(side='left')
        self._post_var = tk.StringVar(value='Nothing')
        ctk.CTkOptionMenu(after_row,
                          values=['Nothing', 'Open output folder', 'Move source to _done/', 'Notify only'],
                          variable=self._post_var,
                          fg_color=SURFACE2, button_color=BORDER,
                          button_hover_color=BORDER,
                          dropdown_fg_color=SURFACE,
                          text_color=TEXT2, font=('', 11), width=180
                          ).pack(side='left', padx=(4, 0))

        # ── OUTPUT FILENAME ──
        self._slabel(row, 'OUTPUT FILENAME'); row += 1
        f = self._card(row); row += 1
        f.grid_columnconfigure(1, weight=1)
        f.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(f, text='Prefix', font=('', 10), text_color=TEXT2,
                     anchor='w').grid(row=0, column=0, padx=(8, 2), pady=(8, 2), sticky='w')
        self._prefix_var = tk.StringVar()
        ctk.CTkEntry(f, textvariable=self._prefix_var,
                     placeholder_text='e.g.  SHOW_',
                     height=28, font=('', 11),
                     fg_color=SURFACE2, border_color=BORDER,
                     text_color=TEXT).grid(row=0, column=1, sticky='ew', padx=(0, 8), pady=(8, 2))

        ctk.CTkLabel(f, text='filename', font=('', 10), text_color=TEXT2,
                     anchor='w').grid(row=1, column=0, padx=(8, 2), pady=2, sticky='w')
        self._fn_preview = ctk.CTkLabel(f, text='', font=('Courier', 10),
                                        text_color=TEXT2, anchor='w')
        self._fn_preview.grid(row=1, column=1, sticky='ew', padx=(0, 8), pady=2)

        ctk.CTkLabel(f, text='Suffix', font=('', 10), text_color=TEXT2,
                     anchor='w').grid(row=2, column=0, padx=(8, 2), pady=(2, 8), sticky='w')
        self._suffix_var = tk.StringVar(value='')
        ctk.CTkEntry(f, textvariable=self._suffix_var,
                     placeholder_text='e.g.  _v2',
                     height=28, font=('', 11),
                     fg_color=SURFACE2, border_color=BORDER,
                     text_color=TEXT).grid(row=2, column=1, sticky='ew', padx=(0, 8), pady=(2, 8))

        self._prefix_var.trace_add('write', lambda *_: self._update_fn_preview())
        self._suffix_var.trace_add('write', lambda *_: self._update_fn_preview())

        ctk.CTkButton(f, text='Batch Rename Sources…', height=28,
                      fg_color=SURFACE2, hover_color=BORDER,
                      text_color=TEXT2, font=('', 11),
                      border_width=1, border_color=BORDER,
                      command=self._batch_rename_dialog
                      ).grid(row=3, column=0, columnspan=4, sticky='ew',
                             padx=8, pady=(0, 8))

        # ── HAP VARIANT ──
        self._slabel(row, 'HAP VARIANT'); row += 1
        vc = self._card(row); row += 1
        vc.grid_columnconfigure(tuple(range(len(HAP_VARIANTS))), weight=1)

        self._variant_var  = tk.StringVar(value='Hap')
        self._variant_btns = {}
        for i, (name, meta) in enumerate(HAP_VARIANTS.items()):
            is_ext = meta['flag'] is None
            sel    = (name == 'Hap')
            card   = _VariantCard(vc, name, meta['tex'], meta['info'],
                                  is_external=is_ext, selected=sel,
                                  command=lambda n=name: self._select_variant(n))
            card.grid(row=0, column=i, padx=(8 if i == 0 else 2, 2 if i < len(HAP_VARIANTS)-1 else 8),
                      sticky='nsew', pady=8)
            self._variant_btns[name] = card

        # Hap R notice banner
        self._hapr_notice = ctk.CTkFrame(vc, fg_color='#2A1F00', corner_radius=6,
                                         border_width=1, border_color='#4A3800')
        self._hapr_notice_lbl = ctk.CTkLabel(
            self._hapr_notice,
            text='Hap R uses BC7 — encoded natively on your Mac.',
            font=('', 11), text_color=AMBER, justify='left', anchor='w',
            wraplength=420)
        self._hapr_notice_lbl.pack(side='left', padx=10, pady=6)
        self._hapr_build_btn = ctk.CTkButton(
            self._hapr_notice, text='Build Hap R Encoder (one-time setup) →',
            height=30, font=('', 11),
            fg_color='#5A4800', hover_color='#4A3800',
            text_color=AMBER, corner_radius=6,
            command=self._hapr_setup)
        self._hapr_build_btn.pack(side='right', padx=(0, 8), pady=6)

        # ── VIDEO OPTIONS ──
        self._slabel(row, 'VIDEO OPTIONS'); row += 1
        f = self._card(row); row += 1
        f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(f, text='Frame rate', font=('', 11), text_color=TEXT2,
                     anchor='w').grid(row=0, column=0, padx=(8, 4), pady=6, sticky='w')
        self._fps_var = tk.StringVar(value='Original')
        ctk.CTkOptionMenu(f, values=FRAME_RATES, variable=self._fps_var,
                          fg_color=SURFACE2, button_color=BORDER,
                          button_hover_color=BORDER,
                          dropdown_fg_color=SURFACE,
                          text_color=TEXT, font=('', 11),
                          command=lambda _: self._update_fn_preview()
                          ).grid(row=0, column=1, sticky='ew', padx=(0, 8), pady=6)

        ctk.CTkLabel(f, text='Resolution', font=('', 11), text_color=TEXT2,
                     anchor='w').grid(row=1, column=0, padx=(8, 4), pady=6, sticky='w')
        self._res_var = tk.StringVar(value='Original')
        self._res_menu = ctk.CTkOptionMenu(f, values=RESOLUTIONS, variable=self._res_var,
                                           fg_color=SURFACE2, button_color=BORDER,
                                           button_hover_color=BORDER,
                                           dropdown_fg_color=SURFACE,
                                           text_color=TEXT, font=('', 11),
                                           command=self._on_res_change)
        self._res_menu.grid(row=1, column=1, sticky='ew', padx=(0, 8), pady=6)

        self._custom_res_var   = tk.StringVar()
        self._custom_res_entry = ctk.CTkEntry(f, textvariable=self._custom_res_var,
                                              placeholder_text='e.g. 2560x1440',
                                              height=28, font=('', 11),
                                              fg_color=SURFACE2, border_color=BORDER,
                                              text_color=TEXT)

        alpha_row = ctk.CTkFrame(f, fg_color='transparent')
        alpha_row.grid(row=2, column=0, columnspan=2, sticky='ew', padx=8, pady=(0, 4))
        ctk.CTkLabel(alpha_row, text='Alpha', font=('', 11),
                     text_color=TEXT2).pack(side='left')
        self._premul_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(alpha_row, text='Pre-multiply → straight',
                        variable=self._premul_var,
                        font=('', 11), text_color=TEXT,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER
                        ).pack(side='left', padx=(8, 0))

        tc_row = ctk.CTkFrame(f, fg_color='transparent')
        tc_row.grid(row=3, column=0, columnspan=2, sticky='ew', padx=8, pady=(0, 4))
        ctk.CTkLabel(tc_row, text='TC burn-in', font=('', 11),
                     text_color=TEXT2).pack(side='left')
        self._tc_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(tc_row, text='Burn timecode',
                        variable=self._tc_var,
                        font=('', 11), text_color=TEXT,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER
                        ).pack(side='left', padx=(8, 0))

        lut_row = ctk.CTkFrame(f, fg_color='transparent')
        lut_row.grid(row=4, column=0, columnspan=2, sticky='ew', padx=8, pady=(0, 8))
        ctk.CTkLabel(lut_row, text='LUT (.cube):', font=('', 11),
                     text_color=TEXT2).pack(side='left')
        self._lut_var   = tk.StringVar()
        self._lut_entry = ctk.CTkEntry(lut_row, textvariable=self._lut_var,
                                       placeholder_text='No LUT (optional)',
                                       height=28, font=('', 11), width=160,
                                       fg_color=SURFACE2, border_color=BORDER,
                                       text_color=TEXT)
        self._lut_entry.pack(side='left', padx=(6, 4))
        ctk.CTkButton(lut_row, text='…', width=28, height=28,
                      fg_color=SURFACE2, hover_color=BORDER,
                      text_color=TEXT2, font=('', 11),
                      border_width=1, border_color=BORDER,
                      command=self._browse_lut).pack(side='left')

        # ── ENCODING OPTIONS ──
        self._slabel(row, 'ENCODING OPTIONS'); row += 1
        f = self._card(row); row += 1
        f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(f, text='Audio', font=('', 11), text_color=TEXT2,
                     anchor='w').grid(row=0, column=0, padx=(8, 4), pady=6, sticky='w')
        self._audio_var = tk.StringVar(value='No audio')
        ctk.CTkOptionMenu(f, values=list(AUDIO_MODES.keys()),
                          variable=self._audio_var,
                          fg_color=SURFACE2, button_color=BORDER,
                          button_hover_color=BORDER,
                          dropdown_fg_color=SURFACE,
                          text_color=TEXT, font=('', 11)
                          ).grid(row=0, column=1, sticky='ew', padx=(0, 8), pady=6)

        ctk.CTkLabel(f, text='Parallel encodes', font=('', 11), text_color=TEXT2,
                     anchor='w').grid(row=1, column=0, padx=(8, 4), pady=6, sticky='w')
        self._threads_var = tk.StringVar(value='1 (sequential)')
        ctk.CTkOptionMenu(f, values=THREAD_OPTS, variable=self._threads_var,
                          fg_color=SURFACE2, button_color=BORDER,
                          button_hover_color=BORDER,
                          dropdown_fg_color=SURFACE,
                          text_color=TEXT, font=('', 11)
                          ).grid(row=1, column=1, sticky='ew', padx=(0, 8), pady=6)

        ctk.CTkLabel(f, text='Chunks per frame', font=('', 11), text_color=TEXT2,
                     anchor='w').grid(row=2, column=0, padx=(8, 4), pady=6, sticky='w')
        self._chunks_var = tk.StringVar(value=CHUNK_OPTS[0])
        ctk.CTkOptionMenu(f, values=CHUNK_OPTS, variable=self._chunks_var,
                          fg_color=SURFACE2, button_color=BORDER,
                          button_hover_color=BORDER,
                          dropdown_fg_color=SURFACE,
                          text_color=TEXT, font=('', 11)
                          ).grid(row=2, column=1, sticky='ew', padx=(0, 8), pady=6)

        ctk.CTkLabel(f, text='Extra ffmpeg flags', font=('', 11), text_color=TEXT2,
                     anchor='w').grid(row=3, column=0, padx=(8, 4), pady=6, sticky='w')
        self._extra_var = tk.StringVar()
        ctk.CTkEntry(f, textvariable=self._extra_var,
                     placeholder_text='-b:v 500M',
                     height=28, font=('', 11),
                     fg_color=SURFACE2, border_color=BORDER,
                     text_color=TEXT
                     ).grid(row=3, column=1, sticky='ew', padx=(0, 8), pady=6)

        opts_row = ctk.CTkFrame(f, fg_color='transparent')
        opts_row.grid(row=4, column=0, columnspan=2, sticky='ew', padx=8, pady=(0, 8))
        self._report_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts_row, text='Save .json report',
                        variable=self._report_var,
                        font=('', 11), text_color=TEXT,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER
                        ).pack(side='left', padx=(0, 12))
        self._verify_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts_row, text='Verify output after encode (probe for corruption)',
                        variable=self._verify_var,
                        font=('', 11), text_color=TEXT,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER
                        ).pack(side='left')

        # ── WATCH FOLDER ──
        self._slabel(row, 'WATCH FOLDER'); row += 1
        f = self._card(row); row += 1
        f.grid_columnconfigure(0, weight=1)
        watch_row = ctk.CTkFrame(f, fg_color='transparent')
        watch_row.grid(row=0, column=0, sticky='ew', padx=8, pady=8)
        watch_row.grid_columnconfigure(0, weight=1)
        self._watch_entry = ctk.CTkEntry(watch_row,
                                          placeholder_text='No watch folder set',
                                          height=28, font=('', 11),
                                          fg_color=SURFACE2, border_color=BORDER,
                                          text_color=TEXT)
        self._watch_entry.grid(row=0, column=0, sticky='ew', padx=(0, 4))
        ctk.CTkButton(watch_row, text='…', width=28, height=28,
                      fg_color=SURFACE2, hover_color=BORDER,
                      text_color=TEXT2, font=('', 11),
                      border_width=1, border_color=BORDER,
                      command=self._browse_watch_dir).grid(row=0, column=1)
        self._watch_btn = ctk.CTkButton(f, text='▶  Start Watching', height=30,
                                         fg_color=SURFACE, hover_color=SURFACE2,
                                         border_width=1, border_color=BORDER,
                                         text_color=TEXT2, font=('', 11),
                                         command=self._toggle_watch)
        self._watch_btn.grid(row=1, column=0, sticky='ew', padx=8, pady=(0, 4))
        self._watch_status = ctk.CTkLabel(f, text='', font=('', 10), text_color=TEXT2)
        self._watch_status.grid(row=2, column=0, sticky='w', padx=8, pady=(0, 6))

        row += 1  # spacer

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_right(self, parent):
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(parent, fg_color='transparent')
        hdr.grid(row=0, column=0, sticky='ew', padx=(12, 16), pady=(24, 0))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text='QUEUE', font=('', 12, 'bold'),
                     text_color=TEXT2).grid(row=0, column=0, sticky='w')
        self._queue_count_lbl = ctk.CTkLabel(hdr, text='0 files',
                                              font=('', 10), text_color=TEXT2)
        self._queue_count_lbl.grid(row=1, column=0, sticky='w')

        self._job_list_frame = ctk.CTkScrollableFrame(
            parent, fg_color=SURFACE2,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT)
        self._job_list_frame.grid(row=1, column=0, sticky='nsew', padx=(12, 16), pady=(8, 0))
        self._job_list_frame.grid_columnconfigure(0, weight=1)

        self._empty_lbl = ctk.CTkLabel(
            self._job_list_frame,
            text=f'No files added yet.\nDrop videos, image sequences or folders here,\nor click ＋ Add  ({MOD_LABEL}O)',
            font=('', 12), text_color=TEXT2, justify='center')
        self._empty_lbl.grid(row=0, column=0, pady=(40, 0))

        summary_card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        summary_card.grid(row=2, column=0, sticky='ew', padx=(12, 16), pady=(6, 0))
        summary_card.grid_columnconfigure(0, weight=1)
        self._summary_lbl = ctk.CTkLabel(summary_card, text='Idle',
                                          font=('', 10), text_color=TEXT2, anchor='w')
        self._summary_lbl.grid(row=0, column=0, padx=10, pady=6, sticky='ew')

        btn_row = ctk.CTkFrame(parent, fg_color='transparent')
        btn_row.grid(row=3, column=0, sticky='ew', padx=(12, 16), pady=(6, 0))
        btn_row.grid_columnconfigure(0, weight=0)
        btn_row.grid_columnconfigure(1, weight=0)
        btn_row.grid_columnconfigure(2, weight=0)
        ctk.CTkButton(btn_row, text='Clear done', width=80, height=28,
                      fg_color=SURFACE, hover_color=SURFACE2,
                      text_color=TEXT2, font=('', 10),
                      border_width=1, border_color=BORDER,
                      command=self._clear_done
                      ).grid(row=0, column=0, padx=(0, 4), pady=(0, 6))
        ctk.CTkButton(btn_row, text='Rename…', width=70, height=28,
                      fg_color=SURFACE, hover_color=SURFACE2,
                      text_color=TEXT2, font=('', 10),
                      border_width=1, border_color=BORDER,
                      command=self._batch_rename_dialog
                      ).grid(row=0, column=1, padx=(0, 4), pady=(0, 6))

        ctk.CTkLabel(parent,
                     text=f'{MOD_LABEL}O add files  •  {MOD_LABEL}{RETURN_LABEL} start  •  {MOD_LABEL}K cancel  •  {MOD_LABEL}R clear done  •  Esc deselect',
                     font=('', 10), text_color=TEXT2
                     ).grid(row=4, column=0, padx=12, pady=(0, 8), sticky='w')

        self._encode_btn = ctk.CTkButton(
            parent, text='Encode Queue', height=44,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color='white', font=('', 14, 'bold'),
            corner_radius=8, state='disabled',
            command=self._start_queue)
        self._encode_btn.grid(row=5, column=0, sticky='ew', padx=(12, 16), pady=(0, 8))

        self._cancel_btn = ctk.CTkButton(
            parent, text='Cancel All', height=34,
            fg_color=SURFACE, hover_color=SURFACE2,
            text_color=TEXT2, font=('', 12),
            border_width=1, border_color=BORDER,
            corner_radius=8, state='disabled',
            command=self._cancel_all)
        self._cancel_btn.grid(row=6, column=0, sticky='ew', padx=(12, 16), pady=(0, 16))

    # ── UI Helpers ────────────────────────────────────────────────────────────

    def _card(self, row: int) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._left, fg_color=SURFACE, corner_radius=10,
                         border_width=1, border_color=BORDER)
        f.grid(row=row, column=0, sticky='ew', padx=16, pady=(0, 4))
        f.grid_columnconfigure(0, weight=1)
        return f

    def _slabel(self, row: int, text: str):
        ctk.CTkLabel(self._left, text=text, font=('', 10), text_color=TEXT2,
                     anchor='w').grid(row=row, column=0, sticky='ew',
                                      padx=(14, 4), pady=(14, 4))

    # ── Settings ──────────────────────────────────────────────────────────────

    def _current_settings(self) -> dict:
        return {
            'variant':     self._variant_var.get(),
            'audio':       self._audio_var.get(),
            'fps':         self._fps_var.get(),
            'resolution':  self._res_var.get(),
            'custom_res':  self._custom_res_var.get(),
            'premultiply': self._premul_var.get(),
            'tc_burnin':   self._tc_var.get(),
            'lut':         self._lut_var.get(),
            'prefix':      self._prefix_var.get(),
            'suffix':      self._suffix_var.get(),
            'extra':       self._extra_var.get(),
            'post_action': self._post_var.get(),
            'report':      self._report_var.get(),
            'verify':      self._verify_var.get(),
            'threads':     self._threads_var.get(),
            'chunks':      self._chunks_var.get(),
            'out_dir':     self._out_dir_entry.get(),
            'watch_dir':   self._watch_entry.get(),
            'settings_version': 2,
        }

    def _apply_settings(self, s: dict):
        # FIX: apply suffix BEFORE calling _select_variant so the variant
        # doesn't overwrite a saved custom suffix
        if 'suffix' in s:
            sfx = s['suffix']
            # v1.5.1 and earlier auto-filled _Hap/_HapQ etc. — drop those
            if s.get('settings_version', 1) < 2 and sfx in set(VARIANT_SUFFIXES.values()):
                sfx = ''
            self._suffix_var.set(sfx)
        if 'variant' in s:
            self._select_variant(s['variant'])
        if 'audio' in s:
            self._audio_var.set(s['audio'])
        if 'fps' in s:
            self._fps_var.set(s['fps'])
        if 'resolution' in s:
            self._res_var.set(s['resolution'])
        if 'custom_res' in s:
            self._custom_res_var.set(s['custom_res'])
        if 'premultiply' in s:
            self._premul_var.set(s['premultiply'])
        if 'tc_burnin' in s:
            self._tc_var.set(s['tc_burnin'])
        if 'lut' in s:
            self._lut_var.set(s['lut'])
        if 'prefix' in s:
            self._prefix_var.set(s['prefix'])
        if 'extra' in s:
            self._extra_var.set(s['extra'])
        if 'post_action' in s:
            self._post_var.set(s['post_action'])
        if 'report' in s:
            self._report_var.set(s['report'])
        if 'verify' in s:
            self._verify_var.set(s['verify'])
        if 'threads' in s:
            self._threads_var.set(s['threads'])
        if 'chunks' in s and s['chunks'] in CHUNK_OPTS:
            self._chunks_var.set(s['chunks'])
        if 'out_dir' in s:
            d = s['out_dir'].strip()
            if d and (not Path(d).is_dir() or Path(d) == Path('/')):
                d = ''
            self._out_dir_entry.delete(0, 'end')
            self._out_dir_entry.insert(0, d)
        if 'watch_dir' in s:
            self._watch_entry.delete(0, 'end')
            self._watch_entry.insert(0, s['watch_dir'])
        self._update_fn_preview()

    def _restore_settings(self):
        if self._saved_settings:
            self._apply_settings(self._saved_settings)
        self.update_idletasks()
        self._defaults = self._panel_job_settings()       # what newly added videos get
        self._panel_last = dict(self._defaults)
        for v in (self._variant_var, self._audio_var, self._fps_var, self._res_var,
                  self._custom_res_var, self._premul_var, self._tc_var, self._lut_var,
                  self._extra_var, self._chunks_var, self._prefix_var, self._suffix_var):
            v.trace_add('write', lambda *_: self._schedule_panel_change())
        self._update_selection_banner()

    def _save_current_settings(self):
        s = self._current_settings()
        if getattr(self, '_selected', None):
            s.update(self._defaults)        # the panel shows a selected video: keep the defaults
        save_settings(s)

    # ── Presets ───────────────────────────────────────────────────────────────

    def _preset_labels(self) -> list:
        return ['Presets'] + sorted(self._presets.keys())

    def _refresh_preset_menu(self):
        self._preset_menu.configure(values=self._preset_labels())

    def _load_preset(self, name: str):
        if name in self._presets:
            self._apply_settings(self._presets[name])

    def _save_preset_dialog(self):
        PresetDialog(self, self._on_preset_saved, self._current_settings())

    def _on_preset_saved(self, name: str, data: dict):
        self._presets[name] = data
        save_presets(self._presets)
        self._refresh_preset_menu()

    # ── Keyboard / drag-drop ──────────────────────────────────────────────────

    def _bind_shortcuts(self):
        for key, method in SHORTCUTS.items():
            self.bind(key, lambda e, k=key, m=method: self._shortcut(e, k, m))

    def _shortcut(self, event, key: str, method: str):
        # a shortcut without Cmd/Ctrl never fires while a text field has the keyboard focus
        if MOD_KEY not in key:
            focus = self.focus_get()
            if isinstance(focus, (tk.Entry, tk.Text)):
                return None
        fn = getattr(self, method, None)
        if fn is None:
            return None
        fn(event)
        return 'break'

    def _start_shortcut(self, event=None):
        if not self._running:
            self._start_queue()

    def _setup_drag_drop(self):
        """Files / folders dropped anywhere on the window are added to the queue (tkdnd, via the
        tkinterdnd2 package; Tk itself has no drag and drop). Without tkinterdnd2 the window just
        doesn't accept drops."""
        self._dnd_ok = False
        if TkinterDnD is None:
            return
        try:
            TkinterDnD._require(self)
        except Exception:
            return
        def register(w):
            try:
                w.drop_target_register('DND_Files')
                w.dnd_bind('<<Drop>>', self._on_drop)
            except Exception:
                pass
            for c in w.winfo_children():
                register(c)
        register(self)
        self._dnd_ok = True

    def _register_drop(self, widget):
        """Make a widget created after start-up (e.g. a queue row) accept drops too."""
        if not getattr(self, '_dnd_ok', False):
            return
        def register(w):
            try:
                w.drop_target_register('DND_Files')
                w.dnd_bind('<<Drop>>', self._on_drop)
            except Exception:
                pass
            for c in w.winfo_children():
                register(c)
        register(widget)

    def _on_drop(self, event):
        paths = [p for p in self.tk.splitlist(event.data) if p]
        self.after(10, self._add_inputs, paths)    # let the drag finish before any dialog opens
        return getattr(event, 'action', 'copy')

    def _add_inputs(self, paths):
        """Videos, image files (→ their whole numbered sequence) and folders of either."""
        rate = None; seen = set(); singles = []
        def add_seq(r):
            nonlocal rate
            pattern, start, count = r
            if pattern in seen or rate is False:
                return
            seen.add(pattern)
            if rate is None:
                rate = self._ask_seq_rate(pattern, start, count) or False
                if rate is False:
                    return
            register_sequence(pattern, start, count, rate)
            self._add_job(pattern)
        for p in paths:
            P = Path(p)
            if P.is_dir():
                for f in sorted(P.iterdir()):
                    if f.suffix.lower() in VIDEO_EXTS:
                        self._add_job(str(f))
                for r in find_sequences(P):
                    add_seq(r)
            elif P.suffix.lower() in VIDEO_EXTS:
                self._add_job(str(P))
            elif P.suffix.lower() in IMAGE_EXTS:
                r = detect_sequence(str(P))
                if r:
                    add_seq(r)
                else:
                    singles.append(P.name)
        if singles:
            messagebox.showinfo('Not an image sequence',
                                'These images aren\'t part of a numbered sequence '
                                '(e.g. shot_0001.png, shot_0002.png …):\n\n' + '\n'.join(singles[:10]))

    def _ask_seq_rate(self, pattern: str, start: int, count: int) -> Optional[str]:
        fps = self._fps_var.get()
        if fps and fps != 'Original':
            r = parse_rate(fps)
            if r:
                return r
        label = seq_label(pattern) if pattern in SEQUENCES else Path(pattern).name
        default = getattr(self, '_last_seq_rate', '30')
        while True:
            ans = simpledialog.askstring(
                'Image sequence frame rate',
                f'{Path(pattern).parent.name}/\n{count} frames starting at {start}\n\n'
                f'Frame rate (e.g. 24, 25, 29.97, 30, 60):',
                initialvalue=default, parent=self)
            if ans is None:
                return None
            r = parse_rate(ans)
            if r:
                self._last_seq_rate = ans.strip()
                return r
            messagebox.showwarning('Frame rate', f'"{ans}" isn\'t a frame rate.', parent=self)

    # ── Browse ────────────────────────────────────────────────────────────────

    def _browse_files(self, event=None):
        paths = filedialog.askopenfilenames(
            title='Add Videos or Image Sequences',
            filetypes=[('Videos and image sequences',
                        ' '.join(f'*{e}' for e in sorted(VIDEO_EXTS | IMAGE_EXTS))),
                       ('Video files', ' '.join(f'*{e}' for e in sorted(VIDEO_EXTS))),
                       ('Image sequences (pick any frame)',
                        ' '.join(f'*{e}' for e in sorted(IMAGE_EXTS))),
                       ('All files', '*.*')])
        self._add_inputs(paths)

    def _browse_out_dir(self):
        cur = self._out_dir_entry.get().strip()
        init = cur if cur and Path(cur).is_dir() else str(Path.home())
        d = filedialog.askdirectory(parent=self, title='Select output folder',
                                    initialdir=init, mustexist=True)
        if d and Path(d) != Path('/'):
            self._out_dir_entry.delete(0, 'end')
            self._out_dir_entry.insert(0, d)
            self._update_fn_preview()

    def _reset_out_dir(self):
        self._out_dir_entry.delete(0, 'end')
        self._update_fn_preview()

    def _save_job_as(self, job: EncodeJob):
        if job.state != ST_QUEUED:
            return
        cur = Path(job.output_path)
        init_dir = str(cur.parent) if cur.parent.is_dir() else str(Path.home())
        p = filedialog.asksaveasfilename(
            parent=self, title='Save encoded file as',
            initialdir=init_dir, initialfile=cur.name,
            defaultextension='.mov',
            filetypes=[('QuickTime movie', '*.mov')])
        if not p:
            return
        if not p.lower().endswith('.mov'):
            p += '.mov'
        if Path(p).resolve() == Path(job.input_path).resolve():
            messagebox.showwarning('Choose another name',
                                   "That would overwrite the source file. Pick a different name or folder.")
            return
        job.output_path   = p
        job.custom_output = True
        if job.row_widget:
            job.row_widget.refresh_output()

    def _apply_ui_settings(self, job: EncodeJob):
        """Make a queued job use whatever is currently in the settings panel."""
        self._apply_job_settings(job, self._panel_job_settings())

    # ── Per-video settings (1.5.24) ───────────────────────────────────────────
    # Every video in the queue keeps its own settings (job.settings, PER_JOB_KEYS). Click a video in
    # the queue (Cmd/Ctrl- or Shift-click for several) and the settings panel edits those videos;
    # with nothing selected the panel holds the defaults that newly added videos get.

    def _panel_job_settings(self) -> dict:
        s = self._current_settings()
        return {k: s[k] for k in PER_JOB_KEYS}

    def _apply_job_settings(self, job: EncodeJob, s: dict):
        job.settings = dict(s)
        name = s.get('variant') or 'Hap Q'
        if name not in HAP_VARIANTS:
            name = 'Hap Q'
        meta = HAP_VARIANTS[name]
        job.variant_name = name
        if meta['flag'] is None:
            job.variant_flag = 'hapr'
            job.hapr_bin = self.hapr_bin or find_hapr_encoder()
        else:
            job.variant_flag = meta['flag']
        res = s.get('resolution') or 'Original'
        if res == 'Custom…':
            res = (s.get('custom_res') or '').strip() or 'Original'
        job.audio_codec = AUDIO_MODES.get(s.get('audio'), None)
        job.resolution  = res
        job.fps         = s.get('fps') or 'Original'
        job.premultiply = bool(s.get('premultiply'))
        job.lut_path    = s.get('lut') or ''
        job.tc_burnin   = bool(s.get('tc_burnin'))
        job.extra_flags = s.get('extra') or ''
        job.out_dir     = (s.get('out_dir') or '').strip()
        job.prefix      = s.get('prefix') or ''
        job.suffix      = s.get('suffix') or ''
        job.chunks      = chunks_from_label(s.get('chunks'))
        if not job.custom_output:
            job.output_path = make_output_path(job.input_path, job.out_dir,
                                               job.prefix, job.suffix, job.variant_name)
        if job.row_widget:
            job.row_widget.refresh_output()
            job.row_widget.refresh_summary()

    def _job_summary(self, job: EncodeJob) -> str:
        s = job.settings or {}
        res = job.resolution if job.resolution and job.resolution != 'Original' else 'original size'
        fps = f"{job.fps} fps" if job.fps and job.fps != 'Original' else 'original fps'
        parts = [job.variant_name, res, fps]
        if job.audio_codec is None:
            parts.append('no audio')
        if job.chunks:
            parts.append(f'{job.chunks} chunks')
        if job.premultiply:
            parts.append('pre-mult')
        if job.tc_burnin:
            parts.append('TC')
        if job.lut_path:
            parts.append('LUT')
        if job.extra_flags.strip():
            parts.append('extra flags')
        return '  ·  '.join(parts) + ('   ·  edited' if job.customized else '')

    def _schedule_panel_change(self):
        if getattr(self, '_panel_pending', False) or not hasattr(self, '_defaults'):
            return
        self._panel_pending = True
        self.after_idle(self._panel_changed)

    def _panel_changed(self):
        self._panel_pending = False
        if getattr(self, '_panel_loading', False):
            return
        cur = self._panel_job_settings()
        changed = {k: v for k, v in cur.items() if self._panel_last.get(k) != v}
        self._panel_last = cur
        if not changed:
            return
        targets = [j for j in self._selected if j.state == ST_QUEUED]
        if targets:
            for j in targets:                       # only what was changed: other differences stay
                s = dict(j.settings or self._defaults)
                s.update(changed)
                j.customized = True
                self._apply_job_settings(j, s)
        else:
            self._defaults = cur

    def _load_panel(self, s: dict):
        self._panel_loading = True
        try:
            self._apply_settings({k: s[k] for k in PER_JOB_KEYS if k in s})
            self.update_idletasks()
        finally:
            self._panel_loading = False
        self._panel_last = self._panel_job_settings()

    def _select_job(self, job: EncodeJob, event=None):
        if job.state != ST_QUEUED:
            return
        state = getattr(event, 'state', 0) or 0
        additive = bool(state & (0x0008 if IS_MAC else 0x0004))       # Cmd / Ctrl
        ranged = bool(state & 0x0001) and bool(self._selected)       # Shift
        if ranged:
            queued = [j for j in self._jobs if j.state == ST_QUEUED]
            a, b = queued.index(self._selected[-1]), queued.index(job)
            lo, hi = min(a, b), max(a, b)
            sel = list(self._selected) + [j for j in queued[lo:hi + 1] if j not in self._selected]
        elif additive:
            sel = [j for j in self._selected if j is not job] if job in self._selected \
                  else list(self._selected) + [job]
        else:
            sel = [] if self._selected == [job] else [job]
        self._set_selection(sel)

    def _set_selection(self, sel: list):
        was_empty = not self._selected
        self._selected = [j for j in sel if j in self._jobs and j.state == ST_QUEUED]
        if self._selected:
            self._load_panel(self._selected[-1].settings or self._defaults)
        elif not was_empty:
            self._load_panel(self._defaults)
        for j in self._jobs:
            if j.row_widget:
                j.row_widget.set_selected(j in self._selected)
        self._update_selection_banner()

    def _clear_selection(self, event=None):
        if self._selected:
            self._set_selection([])

    def _apply_panel_to_all(self):
        s = self._panel_job_settings()
        for j in self._jobs:
            if j.state == ST_QUEUED:
                j.customized = False
                self._apply_job_settings(j, s)

    def _update_selection_banner(self):
        if not hasattr(self, '_sel_label'):
            return
        n = len(self._selected)
        if n == 0:
            self._sel_label.configure(text='Settings for newly added videos', text_color=TEXT2)
            self._sel_btn.configure(text='Apply to all queued', command=self._apply_panel_to_all)
            self._sel_frame.configure(border_color=BORDER)
        else:
            what = seq_label(self._selected[0].input_path) if n == 1 else f'{n} videos'
            self._sel_label.configure(text=f'Editing {what}', text_color=ACCENT)
            self._sel_btn.configure(text='Done', command=self._clear_selection)
            self._sel_frame.configure(border_color=ACCENT)

    def _refresh_output_paths(self):
        # 1.5.24: output folder / prefix / suffix are per video now (see _panel_changed)
        self._schedule_panel_change()

    def _browse_watch_dir(self):
        d = filedialog.askdirectory(title='Select watch folder')
        if d:
            self._watch_entry.delete(0, 'end')
            self._watch_entry.insert(0, d)

    def _browse_lut(self):
        p = filedialog.askopenfilename(
            title='Select LUT', filetypes=[('LUT files', '*.cube'), ('All files', '*.*')])
        if p:
            self._lut_var.set(p)

    # ── Resolution ────────────────────────────────────────────────────────────

    def _on_res_change(self, choice: str):
        if choice == 'Custom…':
            self._custom_res_entry.grid(row=2, column=0, columnspan=2,
                                        sticky='ew', padx=8, pady=(0, 6))
        else:
            self._custom_res_entry.grid_remove()
        self._update_fn_preview()

    # ── Filename preview ──────────────────────────────────────────────────────

    def _update_fn_preview(self):
        pre = self._prefix_var.get()
        sfx = self._suffix_var.get()
        vsfx = VARIANT_SUFFIXES.get(self._variant_var.get(), '') if hasattr(self, '_variant_var') else ''
        self._fn_preview.configure(text=f'{pre}filename{sfx}{vsfx}.mov')
        if hasattr(self, '_out_dir_entry'):
            self._schedule_panel_change()

    # ── Variant selection ─────────────────────────────────────────────────────

    def _select_variant(self, name: str):
        self._variant_var.set(name)
        for n, card in self._variant_btns.items():
            card.set_selected(n == name)

        self._update_fn_preview()
        is_r = (name == 'Hap R')
        if is_r:
            self._hapr_notice.grid(row=1, column=0, padx=12, pady=(0, 10),
                                   sticky='ew', columnspan=len(HAP_VARIANTS))
            if self.hapr_bin or find_hapr_encoder():
                self._update_encode_btn()
                return
            else:
                self._encode_btn.configure(state='disabled',
                                           text='Build Hap R encoder first')
                return
        else:
            self._hapr_notice.grid_remove()
            self._update_encode_btn()

    def _update_encode_btn(self):
        queued = [j for j in self._jobs if j.state == ST_QUEUED]
        can = bool(queued) and bool(self.ffmpeg_bin) and not self._running
        self._encode_btn.configure(
            state='normal' if can else 'disabled',
            text='Encode Queue')

    def _toggle_appearance(self):
        mode = 'light' if ctk.get_appearance_mode() == 'Dark' else 'dark'
        ctk.set_appearance_mode(mode)
        self._appearance_mode.set(mode)

    # ── Job management ────────────────────────────────────────────────────────

    def _add_job(self, input_path: str):
        existing = [j.input_path for j in self._jobs
                    if j.state in (ST_QUEUED, ST_ENCODING)]
        if input_path in existing:
            return

        defaults = dict(self._defaults)
        variant_name = defaults.get('variant') or self._variant_var.get()
        meta         = HAP_VARIANTS.get(variant_name, HAP_VARIANTS['Hap Q'])

        hapr_bin = self.hapr_bin or find_hapr_encoder()
        if meta['flag'] is None and not hapr_bin:
            messagebox.showwarning('Hap R not ready',
                                   'Build the encoder first.\n'
                                   'Run: bash hapr_encoder/build_hapr.sh')
            return

        job = EncodeJob(input_path=input_path, output_path='', variant_flag='',
                        audio_codec=None, resolution='Original', fps='Original')
        self._apply_job_settings(job, defaults)
        job.hapr_bin = hapr_bin
        self._jobs.append(job)

        threading.Thread(target=lambda j=job: self._probe_job(j),
                         daemon=True).start()
        self._add_job_row(job)
        self._refresh_queue_ui()

    def _probe_job(self, job: EncodeJob):
        info = probe_info(job.input_path)
        job.input_info = info
        job.duration   = info.get('duration') or None
        reason = unsupported_source_reason(info)
        if reason and job.state == ST_QUEUED:
            job.state, job.status_text = ST_ERROR, reason[0]
            job.error_detail = reason[1]
            def _ui(j=job):
                if j.row_widget:
                    j.row_widget.update_state()
                self._refresh_queue_ui()
            self.after(0, _ui)

    def _add_job_row(self, job: EncodeJob):
        self._empty_lbl.grid_remove()
        widget = JobRow(self._job_list_frame, job,
                        on_remove    = self._remove_job,
                        on_move_up   = self._move_up,
                        on_move_down = self._move_down,
                        on_preview   = self._show_preview,
                        on_save_as   = self._save_job_as,
                        on_select    = self._select_job,
                        summary      = self._job_summary)
        widget.grid(row=len(self._jobs) - 1, column=0,
                    sticky='ew', pady=(0, 6))
        job.row_widget = widget
        self._register_drop(widget)

    def _remove_job(self, job: EncodeJob):
        if job.state == ST_ENCODING:
            return
        self._jobs = [j for j in self._jobs if j is not job]
        if job in self._selected:
            self._set_selection([j for j in self._selected if j is not job])
        self._rebuild_job_rows()
        self._refresh_queue_ui()

    def _move_up(self, job: EncodeJob):
        i = self._jobs.index(job)
        if i > 0:
            self._jobs[i], self._jobs[i-1] = self._jobs[i-1], self._jobs[i]
            self._rebuild_job_rows()

    def _move_down(self, job: EncodeJob):
        i = self._jobs.index(job)
        if i < len(self._jobs) - 1:
            self._jobs[i], self._jobs[i+1] = self._jobs[i+1], self._jobs[i]
            self._rebuild_job_rows()

    def _rebuild_job_rows(self):
        for widget in self._job_list_frame.winfo_children():
            if isinstance(widget, JobRow):
                widget.destroy()
        for i, job in enumerate(self._jobs):
            if job.row_widget:
                job.row_widget.destroy()
            widget = JobRow(self._job_list_frame, job,
                            on_remove    = self._remove_job,
                            on_move_up   = self._move_up,
                            on_move_down = self._move_down,
                            on_preview   = self._show_preview,
                            on_save_as   = self._save_job_as,
                            on_select    = self._select_job,
                            summary      = self._job_summary)
            widget.grid(row=i, column=0, sticky='ew', pady=(0, 6))
            job.row_widget = widget
            widget.set_selected(job in self._selected)
            self._register_drop(widget)

    def _clear_done(self, event=None):
        self._jobs = [j for j in self._jobs
                      if j.state not in (ST_DONE, ST_ERROR, ST_CANCELLED)]
        self._set_selection([j for j in self._selected if j in self._jobs])
        self._rebuild_job_rows()
        self._refresh_queue_ui()

    def _show_preview(self, job: EncodeJob):
        FilePreviewDialog(self, job)

    def _batch_rename_dialog(self):
        jobs = [j for j in self._jobs if j.state == ST_QUEUED]
        if jobs:
            BatchRenameDialog(self, jobs, self._refresh_queue_ui)

    # ── Queue UI ──────────────────────────────────────────────────────────────

    def _refresh_queue_ui(self):
        queued   = sum(1 for j in self._jobs if j.state == ST_QUEUED)
        encoding = sum(1 for j in self._jobs if j.state == ST_ENCODING)
        done     = sum(1 for j in self._jobs if j.state == ST_DONE)
        errors   = sum(1 for j in self._jobs if j.state == ST_ERROR)
        total    = len(self._jobs)

        n = total
        count_text = f'{n} file{"s" if n != 1 else ""}'
        self._queue_count_lbl.configure(text=count_text)

        if not self._jobs:
            self._empty_lbl.grid(row=0, column=0, pady=(40, 0))
        else:
            self._empty_lbl.grid_remove()

        parts = []
        if encoding: parts.append(f'Encoding: {encoding}')
        if queued:   parts.append(f'Queued: {queued}')
        if done:     parts.append(f'Done: {done}')
        if errors:   parts.append(f'Errors: {errors}')
        self._summary_lbl.configure(text='  '.join(parts) if parts else 'Idle')

        variant = self._variant_var.get()
        can = (bool(queued) and
               not self._running and
               bool(self.ffmpeg_bin) and
               (variant != 'Hap R' or bool(self.hapr_bin or find_hapr_encoder())))

        self._encode_btn.configure(
            state='normal' if can else 'disabled',
            text='Encode Queue' if variant != 'Hap R' or (self.hapr_bin or find_hapr_encoder())
            else 'Build Hap R encoder first')
        self._cancel_btn.configure(
            state='normal' if (encoding or queued) else 'disabled')

    def _get_thread_count(self) -> int:
        v = self._threads_var.get()
        try:
            return int(v.split()[0])
        except Exception:
            return 1

    # ── Encode queue ──────────────────────────────────────────────────────────

    def _start_queue(self):
        if self._running:
            return
        queued = [j for j in self._jobs if j.state == ST_QUEUED]
        if not queued:
            return
        self._clear_selection()
        for j in queued:                    # 1.5.24: each video encodes with its own settings
            self._apply_job_settings(j, j.settings or self._defaults)
        self._running = True
        self._encode_btn.configure(state='disabled')
        self._cancel_btn.configure(state='normal')
        self._save_current_settings()
        n_threads = self._get_thread_count()
        self._executor = ThreadPoolExecutor(max_workers=n_threads)
        t = threading.Thread(target=self._queue_worker,
                             args=(queued,), daemon=True)
        t.start()

    def _queue_worker(self, jobs: list):
        report_entries = []
        queue_start    = time.time()
        futures: dict  = {}
        job_queue      = list(jobs)
        n_threads      = self._get_thread_count()

        while job_queue or futures:
            while job_queue and len(futures) < n_threads:
                job = job_queue.pop(0)
                if job.state == ST_CANCELLED:
                    continue
                self.after(0, self._job_started, job)
                f = self._executor.submit(self._run_job_wrapper, job)
                futures[f] = job

            done_futures = [f for f in list(futures.keys()) if f.done()]
            for f in done_futures:
                job = futures.pop(f)
                try:
                    success, message = f.result()
                except Exception as e:
                    success, message = False, str(e)
                entry = self._job_report_entry(job)
                report_entries.append(entry)
                self.after(0, self._job_finished, job, success, message)

            if not done_futures:
                time.sleep(0.05)

        self.after(0, self._queue_done, report_entries, time.time() - queue_start)

    def _run_job_wrapper(self, job: EncodeJob) -> tuple:
        if not job.input_info:
            job.input_info = probe_info(job.input_path)
        reason = unsupported_source_reason(job.input_info)
        if reason:
            job.error_detail = reason[1]
            return False, reason[0]
        out_parent = Path(job.output_path).parent
        if not out_parent.is_dir():
            job.error_detail = f'Output folder does not exist:\n{out_parent}'
            return False, 'Output folder missing'
        if job.variant_flag == 'hapr':
            return self._run_hapr_job(job)
        else:
            return self._run_ffmpeg_job(job)

    def _run_ffmpeg_job(self, job: EncodeJob) -> tuple:
        args = build_ffmpeg_args(job.input_path, job.output_path,
                                 job.variant_flag, job.audio_codec,
                                 job.resolution, job.fps, job.premultiply,
                                 job.lut_path, job.tc_burnin, job.extra_flags, job.chunks)
        producer = None
        producer_err: list = []
        if (job.input_info or {}).get('codec_tag') == 'Hap7':
            # Hap R source: ffmpeg can't decode it, so pphap decodes and pipes RGBA frames in
            try:
                producer, w, h, _n = start_hapr_decode(job.input_path)
            except RuntimeError as e:
                job.error_detail = str(e)
                return False, 'Hap R decode failed'
            rate = (job.input_info or {}).get('rate') or '30'
            i = args.index('-i')
            raw_in = ['-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', rate, '-i', '-']
            if job.audio_codec is not None:     # audio straight from the source file
                raw_in += ['-i', job.input_path, '-map', '0:v', '-map', '1:a?']
            args = raw_in + args[i + 2:]
        cmd = [self.ffmpeg_bin, '-y'] + args
        job.cmd_str = ' '.join(cmd)
        if producer:
            job.cmd_str = f'pphap -i {quote_arg(job.input_path)} -f rawvideo -pix_fmt rgba - | ' + job.cmd_str
        time_re = re.compile(r'time=(\d+):(\d+):(\d+)[.,](\d+)')
        stderr_lines: list = []
        t0_enc = time.time()

        try:
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                                    stdin=producer.stdout if producer else None,
                                    stdout=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', bufsize=1)
            if producer:
                producer.stdout.close()         # ffmpeg owns the pipe now
                threading.Thread(target=lambda: producer_err.extend(
                    l.decode(errors='replace').rstrip() for l in producer.stderr if l.strip()),
                    daemon=True).start()
            self._current_procs[job.uid] = proc

            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    stderr_lines.append(line)
                    if len(stderr_lines) > 30:
                        stderr_lines.pop(0)

                if job.state == ST_CANCELLED:
                    proc.terminate()
                    if producer:
                        producer.kill()
                    return False, 'Cancelled'

                m = time_re.search(line)
                if m and job.duration:
                    h, mi, s, cs = int(m[1]), int(m[2]), int(m[3]), int(m[4])
                    elapsed = h*3600 + mi*60 + s + cs/100
                    pct = min(elapsed / job.duration, 0.99)
                    ts  = f'{h:02d}:{mi:02d}:{s:02d}'
                    wall = time.time() - t0_enc
                    job.progress    = pct
                    job.status_text = (f'Encoding… {ts} ({int(pct*100)}%)'
                                       f'  [{format_time(wall)}]')
                    self.after(0, job.row_widget.update_state)

            proc.wait()
            self._current_procs.pop(job.uid, None)
            if producer:
                try:
                    producer.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    producer.kill()
                if producer.returncode not in (0, None) and proc.returncode == 0:
                    job.error_detail = producer_err[-1] if producer_err else f'pphap exit {producer.returncode}'
                    return False, 'Hap R decode failed'

            if proc.returncode == 0:
                job.output_size = (os.path.getsize(job.output_path)
                                   if os.path.exists(job.output_path) else None)
                total_time = format_time(time.time() - t0_enc)
                sz = format_bytes(job.output_size) if job.output_size else ''
                return True, f'Done  {sz}  [{total_time}]'
            else:
                relevant = [l for l in stderr_lines
                            if l and 'ffmpeg version' not in l
                            and 'configuration:' not in l
                            and not l.startswith('  lib')]
                job.full_stderr  = stderr_lines[:]
                job.error_detail = (relevant[-1].strip() if relevant
                                    else f'exit {proc.returncode}')
                if any('no decoder found' in l or 'unknown codec' in l
                       for l in stderr_lines):
                    job.error_detail = ('ffmpeg could not decode the source video. '
                                        'Re-export it to ProRes or H.264 and try again.')
                    return False, 'Source codec not supported'
                return False, f'Error (code {proc.returncode})'

        except Exception as e:
            self._current_procs.pop(job.uid, None)
            job.error_detail = str(e)
            return False, 'Error'

    def _run_pphap_job(self, job: EncodeJob, pphap: str) -> tuple:
        """Hap R through pphap — the PixelPress HAP plugin's encoder, identical output."""
        seq = SEQUENCES.get(job.input_path)
        producer = None
        # audio (pphap 1.1.7+): the source's track as PCM unless the job says "No audio"
        audio = []
        if pphap_version(pphap) >= (1, 1, 7):
            audio = ['-an'] if job.audio_codec is None else ['-c:a', 'pcm_s16le']
        tail = ['-c:v', 'hap_r', '-quality', PPHAP_HAPR_QUALITY] + audio + \
               (['-chunks', str(job.chunks)] if job.chunks > 0 else []) + [
                '-y', '-nostats', '-progress', 'pipe:1', job.output_path]
        # 1.5.14: the job's video options (frame rate, resolution, pre-multiply, LUT, timecode burn-in,
        # extra -vf) apply to Hap R too - before, Hap R jobs silently ignored them
        vf = _job_filters(job)
        if seq:
            # Image sequence: ffmpeg decodes the frames straight to RGBA and pipes them
            # in, so pphap gets the exact pixels (no YUV step) and any start number works.
            info = job.input_info or probe_info(job.input_path)
            w, h = info.get('width'), info.get('height')
            ffmpeg = find_ffmpeg()
            if not (w and h and ffmpeg):
                job.error_detail = 'Could not read the image sequence (size unknown or ffmpeg missing).'
                return False, 'Sequence error'
            rate = seq['rate']
            if vf:                          # the sequence's ffmpeg applies them; work out the new size
                if job.resolution and 'x' in job.resolution and job.resolution != 'Original':
                    w, h = (int(x) for x in job.resolution.split('x'))
                w, h = (w + 3) // 4 * 4, (h + 3) // 4 * 4
                if job.fps and job.fps not in ('Original', ''):
                    rate = SEQ_RATE_ALIASES.get(job.fps, job.fps)
            src_cmd = [ffmpeg, '-v', 'error', '-nostdin'] + seq_input_args(job.input_path) + \
                      ['-i', job.input_path, '-frames:v', str(seq['count'])] + \
                      (['-vf', vf] if vf else []) + ['-f', 'rawvideo', '-pix_fmt', 'rgba', '-']
            cmd = [pphap, '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}',
                   '-r', rate, '-i', '-'] + tail
            job.cmd_str = quote_cmd(src_cmd) + ' | ' + quote_cmd(cmd)
        else:
            cmd = [pphap, '-i', job.input_path] + (['-vf', vf] if vf else []) + tail
            job.cmd_str = quote_cmd(cmd)
        stderr_lines: list = []
        t0 = time.time()
        try:
            if seq:
                producer = subprocess.Popen(src_cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, env=pphap_env())
            proc = subprocess.Popen(cmd, stdin=producer.stdout if producer else None,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8', errors='replace', bufsize=1, env=pphap_env(),
                                    cwd=tempfile.gettempdir())   # pphap writes PPHAP-log.txt here
            if producer:
                producer.stdout.close()     # pphap owns the pipe now
            self._current_procs[job.uid] = proc
            err_t = threading.Thread(
                target=lambda: stderr_lines.extend(l.rstrip() for l in proc.stderr if l.strip()),
                daemon=True)
            err_t.start()
            for line in proc.stdout:
                if job.state == ST_CANCELLED:
                    stop_process(proc)
                    if producer:
                        producer.kill()
                    proc.wait()
                    self._current_procs.pop(job.uid, None)
                    return False, 'Cancelled'
                k, _, v = line.strip().partition('=')
                pct = None
                try:
                    if k == 'percent' and not seq:
                        pct = float(v) / 100.0
                    elif k == 'frame' and seq:
                        pct = min(1.0, int(v) / max(1, seq['count']))
                except ValueError:
                    pass
                if pct is not None:
                    job.progress = pct
                    job.status_text = (f'Encoding… {int(pct*100)}%'
                                       f'  [{format_time(time.time() - t0)}]')
                    self.after(0, job.row_widget.update_state)
            proc.wait()
            err_t.join(timeout=2)
            if producer:
                producer.wait()
                perr = producer.stderr.read().decode(errors='replace').strip()
                if perr:
                    stderr_lines.append('ffmpeg: ' + perr[-800:])
            self._current_procs.pop(job.uid, None)
            if proc.returncode == 0:
                job.output_size = (os.path.getsize(job.output_path)
                                   if os.path.exists(job.output_path) else None)
                sz = format_bytes(job.output_size) if job.output_size else ''
                return True, f'Done  {sz}  [{format_time(time.time() - t0)}]'
            stderr_lines.append(f'[exit code {proc.returncode}]')
            job.full_stderr = stderr_lines
            job.error_detail = '\n'.join(stderr_lines[-12:])
            return False, f'Hap R error (code {proc.returncode})'
        except Exception as e:
            self._current_procs.pop(job.uid, None)
            job.error_detail = str(e)
            return False, 'Hap R error'

    def _run_hapr_job(self, job: EncodeJob) -> tuple:
        hapr = job.hapr_bin or self.hapr_bin or find_hapr_encoder()
        if not hapr:
            job.error_detail = 'hapr_enc not found. Run build_hapr.sh'
            return False, 'Hap R not built'

        if Path(hapr).name.startswith('pphap'):
            return self._run_pphap_job(job, hapr)
        if job.input_path in SEQUENCES:
            job.error_detail = 'Image sequences need the bundled pphap encoder for Hap R.'
            return False, 'Hap R: sequences need pphap'
        cmd = [hapr, job.input_path, job.output_path]
        job.cmd_str = ' '.join(cmd)
        prog_re      = re.compile(r'PROGRESS\s+([\d.]+)')
        stderr_lines: list = []
        t0_hapr = time.time()

        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith('DYLD_')
                     and k not in ('_MEIPASS', '_MEIPASS2')}
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                                    bufsize=1, env=clean_env)
            self._current_procs[job.uid] = proc

            for line in proc.stdout:
                line = line.rstrip()
                if job.state == ST_CANCELLED:
                    proc.terminate()
                    return False, 'Cancelled'
                m = prog_re.match(line)
                if m:
                    pct = float(m.group(1))
                    job.progress    = pct
                    elapsed = format_time(time.time() - t0_hapr)
                    job.status_text = (f'Encoding… {int(pct*100)}%'
                                       f'  [{elapsed}]')
                    self.after(0, job.row_widget.update_state)

            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    stderr_lines.append(line)

            proc.wait()
            self._current_procs.pop(job.uid, None)

            if proc.returncode == 0:
                job.output_size = (os.path.getsize(job.output_path)
                                   if os.path.exists(job.output_path) else None)
                total_time = format_time(time.time() - t0_hapr)
                sz = format_bytes(job.output_size) if job.output_size else ''
                return True, f'Done  {sz}  [{total_time}]'
            else:
                stderr_lines.append(f'[exit code {proc.returncode}]')
                job.full_stderr  = stderr_lines
                job.error_detail = (stderr_lines[-1] if stderr_lines
                                    else f'exit {proc.returncode}')
                return False, f'Hap R error (code {proc.returncode})'

        except Exception as e:
            self._current_procs.pop(job.uid, None)
            job.error_detail = str(e)
            return False, 'Hap R error'

    def _job_started(self, job: EncodeJob):
        job.state       = ST_ENCODING
        job.status_text = 'Starting…'
        job.progress    = 0.0
        if job.row_widget:
            job.row_widget.update_state()
        self._refresh_queue_ui()

    def _job_finished(self, job: EncodeJob, success: bool, message: str):
        if message == 'Cancelled':
            job.state = ST_CANCELLED
        elif success:
            job.state = ST_DONE
        else:
            job.state = ST_ERROR
        job.status_text = message
        job.progress    = 1.0
        if job.row_widget:
            job.row_widget.update_state()
        self._refresh_queue_ui()

    def _queue_done(self, report_entries: list, total_time: float):
        self._running = False
        self._executor = None
        self._refresh_queue_ui()

        done = sum(1 for j in self._jobs if j.state == ST_DONE)
        errs = sum(1 for j in self._jobs if j.state == ST_ERROR)
        total = done + errs

        err_str = f' error{"s" if errs != 1 else ""}' if errs else ''
        summary = (f'All {total} file{"s" if total != 1 else ""} encoded'
                   f' in {format_time(total_time)}{err_str}')
        notify_macos('PixelPress', summary)

        action = self._post_var.get()
        if action == 'Open output folder':
            for j in self._jobs:
                if j.state == ST_DONE:
                    open_path(str(Path(j.output_path).parent))
                    break
        elif action == 'Move source to _done/':
            for j in self._jobs:
                if j.state == ST_DONE:
                    d = Path(j.input_path).parent / '_done'
                    d.mkdir(exist_ok=True)
                    try:
                        Path(j.input_path).rename(d / Path(j.input_path).name)
                    except Exception:
                        pass

        if self._report_var.get():
            self._save_report(report_entries)
        self._save_current_settings()

    def _job_report_entry(self, job: EncodeJob) -> dict:
        return {
            'input':    job.input_path,
            'output':   job.output_path,
            'state':    job.state,
            'variant':  job.variant_flag,
            'size':     job.output_size,
            'cmd':      job.cmd_str,
            'error':    job.error_detail,
        }

    def _save_report(self, entries: list):
        ts   = time.strftime('%Y%m%d_%H%M%S')
        path = Path.home() / f'pixelpress_report_{ts}.json'
        try:
            path.write_text(json.dumps(entries, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _cancel_all(self, event=None):
        for job in self._jobs:
            if job.state == ST_QUEUED:
                job.state       = ST_CANCELLED
                job.status_text = 'Cancelled'
                if job.row_widget:
                    job.row_widget.update_state()
        for uid, proc in list(self._current_procs.items()):
            if proc.poll() is None:
                proc.terminate()
        self._refresh_queue_ui()

    # ── Watch folder ──────────────────────────────────────────────────────────

    def _toggle_watch(self):
        if self._watch_active:
            self._watch_active = False
            self._watch_btn.configure(text='▶  Start Watching')
            self._watch_status.set('')
            return

        wd = self._watch_entry.get().strip()
        if not wd or not os.path.isdir(wd):
            messagebox.showwarning('Watch folder',
                                   'Please set a valid watch folder first.')
            return

        self._watch_active = True
        self._watch_btn.configure(text='⏹  Stop Watching')
        # seed seen with existing files
        self._watch_seen = {str(f) for f in Path(wd).iterdir()}
        t = threading.Thread(target=self._watch_worker, args=(wd,), daemon=True)
        t.start()

    def _watch_worker(self, wd: str):
        while self._watch_active:
            current = set(str(f) for f in Path(wd).iterdir())
            new     = sorted(current - self._watch_seen)
            self._watch_seen = current

            for f in new:
                if Path(f).suffix.lower() in VIDEO_EXTS:
                    time.sleep(0.5)
                    self.after(0, self._add_job, str(f))
                    self.after(500, self._watch_status.configure,
                               {'text': f'Last: {Path(f).name}'})
                    if not self._running:
                        self.after(500, self._start_queue)

            time.sleep(0.5)

    # ── Hap R setup ───────────────────────────────────────────────────────────

    def _hapr_setup(self):
        if self.hapr_bin or find_hapr_encoder():
            return
        if IS_WIN:
            messagebox.showinfo('Hap R encoder missing',
                                'pphap.exe was not found. Reinstall PixelPress, or install the '
                                'PixelPress HAP plugin (it puts pphap.exe in C:\\Program Files\\PixelPress).')
            return
        here  = Path(__file__).parent
        build = here / 'hapr_encoder' / 'build_hapr.sh'
        if not messagebox.askyesno('Build Hap R Encoder',
                                   'Needs to compile once (~30s). Requires Xcode Command Line Tools.\n'
                                   'Run now?'):
            return
        subprocess.Popen(['open', '-a', 'Terminal', str(build)])
        messagebox.showinfo('Building…',
                            'Restart PixelPress when done.\n'
                            'Title bar will show  Hap R ✓')

    def _check_ffmpeg(self):
        ht = ('  Hap R ✓' if self.hapr_bin else '  Hap R: encoder missing')
        if self.ffmpeg_bin and self.hapr_bin:
            self._ffmpeg_badge.configure(text=f'  ffmpeg ✓{ht}',
                                         text_color=GREEN, fg_color=SURFACE2)
        elif self.ffmpeg_bin:
            self._ffmpeg_badge.configure(text=f'  ffmpeg ✓{ht}',
                                         text_color=AMBER, fg_color=SURFACE2)
        else:
            self._ffmpeg_badge.configure(text='  ⚠ ffmpeg not found  ',
                                         text_color=AMBER, fg_color='#2A1F00')

        for name, card in self._variant_btns.items():
            card.set_native(name == 'Hap R' and bool(self.hapr_bin))

        if hasattr(self, '_hapr_build_btn'):
            if self.hapr_bin:
                self._hapr_build_btn.pack_forget()
                if hasattr(self, '_hapr_notice_lbl'):
                    self._hapr_notice_lbl.configure(
                        text=('Hap R (BC7) — PixelPress HAP encoder (same output as the plugin).'
                              if Path(self.hapr_bin).name.startswith('pphap')
                              else 'Hap R (BC7) — legacy encoder.'))
            else:
                pass

        self._update_fn_preview()

    # ── App close ─────────────────────────────────────────────────────────────

    def on_closing(self):
        self._watch_active = False
        self._save_current_settings()
        if self._running:
            if not messagebox.askyesno('Encoding in progress',
                                       'Jobs are running. Quit anyway?'):
                return
            self._cancel_all()
        self.destroy()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def cli_run():
    parser = argparse.ArgumentParser(prog='pixelpress', description=f'PixelPress v{VERSION}')
    parser.add_argument('-i', '--input',   required=True, help='Input file or folder')
    parser.add_argument('-v', '--variant', default='hap_q',
                        help='Variant: hap, hap_alpha/alpha, hap_q/q, hap_r/r')
    parser.add_argument('--output-dir',  default='')
    parser.add_argument('--prefix',      default='')
    parser.add_argument('--suffix',      default='')
    parser.add_argument('--fps',         default='Original')
    parser.add_argument('--resolution',  default='Original')
    parser.add_argument('--premultiply', action='store_true')
    parser.add_argument('--audio',       default='none',
                        choices=['copy', 'pcm', 'none'])
    parser.add_argument('--extra',       default='')
    parser.add_argument('--report',      action='store_true')
    parser.add_argument('--progress',    default='human',
                        choices=['auto', 'human', 'json'])
    parser.add_argument('--version',     action='version', version=VERSION)
    args = parser.parse_args()

    VARIANT_MAP = {
        'hap': 'Hap', 'hap_alpha': 'Hap Alpha', 'alpha': 'Hap Alpha',
        'hap_q': 'Hap Q', 'hapq': 'Hap Q', 'q': 'Hap Q',
        'hap_r': 'Hap R', 'hapr': 'Hap R', 'r': 'Hap R',
    }
    variant_name = VARIANT_MAP.get(args.variant.lower(), 'Hap Q')
    meta         = HAP_VARIANTS[variant_name]

    audio_map = {'copy': 'copy', 'pcm': 'pcm_s16le', 'none': None}
    audio     = audio_map.get(args.audio, None)

    inp = Path(args.input)
    if inp.is_dir():
        inputs = sorted(f for f in inp.iterdir() if f.suffix.lower() in VIDEO_EXTS)
    else:
        inputs = [inp]

    def emit(d: dict):
        if args.progress == 'json':
            print(json.dumps(d), flush=True)
        else:
            msg = d.get('message', '')
            print(msg, flush=True)

    ffmpeg_bin = find_ffmpeg()
    hapr_bin   = find_hapr_encoder()

    for src in inputs:
        suffix_val = args.suffix or VARIANT_SUFFIXES.get(variant_name, '')
        out = make_output_path(str(src), args.output_dir,
                               args.prefix, suffix_val, variant_name)
        emit({'event': 'start', 'input': str(src), 'output': out,
              'variant': variant_name, 'message': f'Encoding {src.name} → {Path(out).name}'})

        if meta['flag'] is None:
            if not hapr_bin:
                emit({'event': 'error', 'message': 'hapr_enc not found. Run build_hapr.sh'})
                sys.exit(2)
            cmd = [hapr_bin, str(src), out]
            clean_env = {k: v for k, v in os.environ.items()
                         if not k.startswith('DYLD_')}
            proc = subprocess.run(cmd, env=clean_env)
        else:
            if not ffmpeg_bin:
                emit({'event': 'error', 'message': 'ffmpeg not found'})
                sys.exit(2)
            cmd = ([ffmpeg_bin, '-y'] +
                   build_ffmpeg_args(str(src), out, meta['flag'], audio,
                                     args.resolution, args.fps, args.premultiply,
                                     '', False, args.extra))
            proc = subprocess.run(cmd)

        if proc.returncode == 0:
            sz = format_bytes(os.path.getsize(out)) if os.path.exists(out) else ''
            emit({'event': 'done', 'output': out, 'size': sz,
                  'message': f'Done  {sz}  → {out}'})
        else:
            emit({'event': 'error', 'message': f'Encode failed (exit {proc.returncode})'})
            sys.exit(proc.returncode)


# ─── ffmpeg-style CLI (pphap-compatible) ──────────────────────────────────────
# Used when PixelPress is launched with ffmpeg-style arguments, e.g.
#   PixelPress -i in.mov -c:v hap_r -y -progress pipe:1 -nostats out.mov
# Same option names, progress keys and exit codes as the plugin's `pphap`, so
# host apps (e.g. Smart Preso) can drive either one the same way.

FF_HAP_CODECS = {'hap': 'Hap', 'hap_alpha': 'Hap Alpha', 'hap_q': 'Hap Q', 'hap_r': 'Hap R'}
FF_PPHAP_ONLY = {'hap_q_alpha', 'hap_alpha_only'}
# Sources hapr_enc (AVFoundation) can read directly; anything else is first
# converted to a ProRes 4444 intermediate with the bundled ffmpeg.
FF_AV_EXTS   = {'.mov', '.mp4', '.m4v'}
FF_RGB_CODECS = {'png', 'apng', 'rawvideo', 'qtrle', 'tiff', 'bmp', 'dpx', 'targa', 'ppm', 'r210',
                 'gif', 'ffv1', 'exr', 'mjpeg_rgb'}
FF_RGB_EXTS   = {'.png', '.tif', '.tiff', '.bmp', '.dpx', '.tga', '.gif', '.exr', '.ppm'}
FF_AV_CODECS = {'h264', 'hevc', 'prores'}   # PNG/Animation/raw in MOV: AVFoundation can't decode
FF_FLAGS     = {'-an', '-vn', '-sn', '-dn', '-shortest', '-hide_banner', '-nostdin',
                '-stats', '-nostats', '-y', '-n', '-psnr', '-legacy', '-report'}
FF_EXIT_OK, FF_EXIT_ERR, FF_EXIT_INT = 0, 1, 255

FF_USAGE = """PixelPress {v} — ffmpeg-style command line (compatible with pphap)

Usage: PixelPress [input options] -i INPUT [output options] OUTPUT

Input
  -i FILE | -          input file, or raw frames on stdin (use with -f/-pix_fmt/-s/-r)
                       Hap R files are read too (decoded by the bundled pphap 1.1.7+)
                       image sequence: -i shot_0001.png (whole numbered sequence, 30 fps
                       unless -framerate N is given) or ffmpeg style
                       -framerate 24 -start_number 1001 -i shot_%04d.png
  -f FMT, -pix_fmt F, -s WxH, -r RATE     input options when placed before -i
  -vf FILTER           ffmpeg video filter (e.g. scale=1920:1080)
  -r RATE, -s WxH      output frame rate / size when placed after -i
  -frames:v N          stop after N frames
  -ss TIME, -to TIME   IN / OUT points in the source (seconds or [HH:]MM:SS[.ms]), frame-accurate
  -t DURATION          length from -ss instead of -to
                       Smart Preso's form also works: -vf "-s 1920:1080 -ss 1.5 -to 4,format=rgba"

Encoding
  -c:v CODEC           hap | hap_alpha | hap_q | hap_r (default hap_r)
                       hap_r, hap_q_alpha, hap_alpha_only run the bundled pphap (the
                       PixelPress HAP plugin's encoder): identical output to the plugin
                       any other ffmpeg codec (prores_ks, h264_videotoolbox, png, ...) converts the
                       input to that codec, e.g. Hap / Hap Q / Hap R -> ProRes
                       no -c:v and OUTPUT not .mov (frame.png, clip.mp4): ffmpeg's codec for that type
  -format FMT          ffmpeg style: -c:v hap -format hap_q
  -chunks N            texture chunks per frame (default: 8 for Hap R, ffmpeg's default otherwise)
  -threads N           passed to ffmpeg
  -c:a CODEC | -an     audio: like ffmpeg, the source's audio comes along unless -an. Hap R /
                       Hap Q Alpha / Hap Alpha-Only (pphap) write it as PCM (pcm_s16le, or
                       -c:a pcm_s32le); hap / hap_alpha / hap_q use -c:a CODEC (default: none)
  -quality fast|normal|high, -alpha, -engine, -colorconv, -colorspace, -color_range,
  -legacy, -psnr       pphap options: used for hap_r / hap_q_alpha / hap_alpha_only

Decoded frames
  -f rawvideo [-pix_fmt rgba|bgra|argb|rgb24] -      raw frames on stdout (after -i; no header,
                       width x height x 4 bytes per frame); e.g. one frame at 12.5 s:
                       PixelPress -ss 12.5 -i clip.mov -frames:v 1 -f rawvideo -pix_fmt rgba -
                       send -progress to pipe:2 or a file when frames go to stdout

Output / progress
  -y | -n              overwrite / never overwrite OUTPUT (default: error if it exists)
  -progress URL        ffmpeg-style key=value progress to pipe:1 (stdout), pipe:2 (stderr) or a file
  -stats_period SEC    progress/stats update interval (default 0.5)
  -nostats             no status line on stderr
  -loglevel LEVEL      quiet | error (default) | warning | info | verbose
  -h, -version, -L     help / version / licences and source code

Progress keys: frame fps total_frames percent out_time_us out_time_ms out_time total_size
               bitrate speed progress=continue|end
Exit codes:    0 success, 1 error, 255 interrupted
"""


class _FFError(Exception):
    pass


def _ff_rational(s: str) -> float:
    try:
        if '/' in s:
            n, d = s.split('/', 1)
            return float(n) / float(d) if float(d) else 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _ff_fmt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = t - h * 3600 - m * 60
    return f'{h:02d}:{m:02d}:{s:09.6f}'


def _ff_clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith('DYLD_') and k not in ('_MEIPASS', '_MEIPASS2')}


class _FFProgress:
    """Writes pphap/ffmpeg-style progress blocks and the human status line."""

    def __init__(self, url: str, stats: bool, period: float):
        self.out = None
        self.owns = False
        if url in ('pipe:1', 'pipe:', '-'):
            self.out = sys.stdout
        elif url == 'pipe:2':
            self.out = sys.stderr
        elif url:
            if url.startswith('file:'):
                url = url[5:]
            self.out = open(url, 'w')
            self.owns = True
        self.stats = stats
        self.period = period
        self.rate = 30.0
        self.total = -1
        self.start = time.monotonic()
        self.last = 0.0
        self.last_len = 0
        self.ended = False

    def emit(self, frames: int, nbytes: int, end: bool = False, force: bool = False):
        now = time.monotonic()
        if not (end or force) and now - self.last < self.period:
            return
        if self.ended:
            return
        self.last = now
        el = now - self.start
        frames = max(0, int(frames))
        out_t = frames / self.rate if self.rate > 0 else 0.0
        fps = frames / el if el > 0 else 0.0
        speed = out_t / el if el > 0 else 0.0
        kbps = nbytes * 8.0 / out_t / 1000.0 if out_t > 0 else 0.0
        pct = min(100.0, 100.0 * frames / self.total) if self.total > 0 else None
        if self.out:
            us = int(round(out_t * 1e6))
            blk = f'frame={frames}\nfps={fps:.2f}\n'
            if pct is not None:
                blk += f'total_frames={self.total}\npercent={pct:.1f}\n'
            blk += (f'out_time_us={us}\nout_time_ms={us}\nout_time={_ff_fmt_time(out_t)}\n'
                    f'total_size={int(nbytes)}\nbitrate={kbps:.1f}kbits/s\nspeed={speed:.3g}x\n'
                    f'progress={"end" if end else "continue"}\n')
            self.out.write(blk)
            self.out.flush()
        if self.stats:
            p = f' ({pct:3.0f}%)' if pct is not None else ''
            line = (f'frame={frames:6d}{p} fps={fps:5.1f} size={int(nbytes)//1024:9d}KiB '
                    f'time={_ff_fmt_time(out_t)[:11]} bitrate={kbps:8.1f}kbits/s speed={speed:5.2f}x')
            cols = shutil.get_terminal_size((0, 0)).columns
            if cols > 1 and len(line) > cols - 1:
                line = line[:cols - 1]
            if len(line) < self.last_len:
                line += ' ' * (self.last_len - len(line))
            self.last_len = len(line)
            sys.stderr.write('\r' + line + ('\n' if end else ''))
            sys.stderr.flush()
        if end:
            self.ended = True

    def close(self):
        if self.owns and self.out:
            self.out.close()


def _ff_parse(argv: list) -> dict:
    o = {'input': None, 'output': None, 'in_opts': [], 'codec': 'hap_r', 'vf': '',
         'out_rate': '', 'out_size': '', 'frames': 0, 'chunks': 0, 'threads': 0,
         'audio': None, 'progress': '', 'period': 0.5, 'stats': True,
         'overwrite': False, 'no_overwrite': False, 'loglevel': 'error', 'extra': [],
         'pphap_opts': [], 'ss': '', 'to': '', 't': '', 'codec_set': False, 'no_audio': False}
    i = 0
    positional = []

    def need():
        nonlocal i
        if i + 1 >= len(argv):
            raise _FFError(f'missing value for {argv[i]}')
        i += 1
        return argv[i]

    while i < len(argv):
        a = argv[i]
        before_input = o['input'] is None
        if a in ('-h', '-help', '--help'):
            o['help'] = True
        elif a in ('-version', '--version'):
            o['version'] = True
        elif a in ('-L', '-licenses', '--licenses'):
            o['licenses'] = True
        elif a == '-i':
            o['input'] = need()
        elif a in ('-f', '-pix_fmt') and before_input:
            o['in_opts'] += [a, need()]
        elif a in ('-s', '-video_size'):
            v = need()
            if before_input:
                o['in_opts'] += ['-video_size', v]
            else:
                o['out_size'] = v
        elif a == '-framerate':                 # input-only option in ffmpeg, wherever it's placed
            o['in_opts'] += ['-framerate', need()]
        elif a == '-r':
            v = need()
            if before_input:
                o['in_opts'] += ['-r', v]
            else:
                o['out_rate'] = v
        elif a in ('-ss', '-to', '-t'):              # in / out points (input seek, frame-accurate)
            o[a[1:]] = need()
        elif a in ('-vf', '-filter:v'):
            o['vf'] = need()
        elif a in ('-frames:v', '-vframes', '-frames'):
            o['frames'] = int(need())
        elif a in ('-c:v', '-vcodec', '-codec:v', '-c', '-format'):
            o['codec'] = need().lower()
            o['codec_set'] = True
        elif a in ('-chunks', '-chunks:v'):
            o['chunks'] = int(need())
        elif a == '-threads':
            o['threads'] = int(need())
        elif a in ('-c:a', '-acodec', '-codec:a'):
            o['audio'] = need()
        elif a == '-an':
            o['audio'] = None
            o['no_audio'] = True
        elif a in ('-quality', '-q:v', '-alpha', '-engine', '-colorconv',
                   '-colorspace', '-color_matrix', '-color_range'):
            o['pphap_opts'] += [a, need()]      # used when pphap does the encode
        elif a in ('-legacy', '-psnr'):
            o['pphap_opts'].append(a)
        elif a in ('-hide_banner', '-nostdin', '-report'):
            pass
        elif a == '-progress':
            o['progress'] = need()
        elif a == '-stats_period':
            o['period'] = max(0.05, float(need()))
        elif a == '-nostats':
            o['stats'] = False
        elif a == '-stats':
            o['stats'] = True
        elif a == '-y':
            o['overwrite'] = True
        elif a == '-n':
            o['no_overwrite'] = True
        elif a in ('-loglevel', '-v'):
            o['loglevel'] = need().split('+')[-1]
        elif a.startswith('-') and len(a) > 1:
            # any other ffmpeg option (-profile:v 3, -pix_fmt yuv422p10le, ...): passed through.
            # It takes a value unless it's a known flag, the next token is another option,
            # or the next token is the last one (the output file).
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            takes = (a not in FF_FLAGS and nxt is not None and i + 1 < len(argv) - 1
                     and not (nxt.startswith('-') and not re.match(r'-\d', nxt)))
            opt = [a, need()] if takes else [a]
            if before_input:
                o['in_opts'] += opt
            else:
                o['extra'] += opt
        else:
            positional.append(a)
        i += 1
    if positional:
        o['output'] = positional[-1]
    _ff_normalize_vf(o)
    for k in ('ss', 'to', 't'):
        if o[k] and _ff_time(o[k]) < 0:
            raise _FFError(f'bad -{k} value: {o[k]}')
    if o['to'] and o['t']:
        raise _FFError('use -to or -t, not both')
    if o['to'] and _ff_time(o['to']) <= (_ff_time(o['ss']) if o['ss'] else 0):
        raise _FFError('-to must be after -ss')
    return o


def _ff_time(v: str) -> float:
    """'117.284', '1:57.284', '00:01:57.284' -> seconds; -1 if malformed."""
    try:
        parts = v.split(':')
        if not 1 <= len(parts) <= 3 or any(p == '' for p in parts):
            return -1
        total = 0.0
        for p in parts:
            x = float(p)
            if x < 0:
                return -1
            total = total * 60 + x
        return total
    except ValueError:
        return -1


def _vf_add_font(vf: str) -> str:
    """drawtext with no font: the bundled ffmpeg has no fontconfig, so it can't pick one itself -
    give it the same font the app's timecode burn-in uses."""
    if 'drawtext' in vf and not re.search(r'(fontfile|font)\s*=', vf):
        f = burnin_font()
        if f:
            vf = re.sub(r'drawtext(=|(?=,|$))',
                        lambda m: f"drawtext=fontfile='{filter_path(f)}'" + (':' if m.group(1) else ''), vf)
    return vf


def _ff_normalize_vf(o: dict):
    """Smart Preso's CLI puts ffmpeg options inside -vf, e.g.
    -vf "-s 1920:1080 -ss 117.284 -to 121.822,format=rgba". Same handling as pphap 1.1.5:
    -s -> scale filter, -ss/-to/-t -> in/out points, -pix_fmt ignored, RGB format= dropped."""
    vf = _vf_add_font(o.get('vf') or '')
    if vf:
        o['vf'] = vf
    if '-' not in vf:
        return
    keep = []
    for item in (x.strip() for x in vf.split(',')):
        if not item:
            continue
        if not item.startswith('-'):
            if item.startswith('format=') and item[7:] in ('rgba', 'bgra', 'argb', 'abgr', 'rgb24', 'bgr24'):
                continue
            keep.append(item)
            continue
        tok = item.split()
        if len(tok) % 2:
            raise _FFError(f'missing value for {tok[-1]} in -vf')
        for opt, val in zip(tok[::2], tok[1::2]):
            if opt in ('-s', '-video_size'):
                w, sep, h = val.replace('x', ':').partition(':')
                if not sep:
                    raise _FFError(f'bad -s value in -vf: {val}')
                keep.append(f'scale={w}:{h}')
            elif opt in ('-ss', '-to', '-t'):
                o[opt[1:]] = o[opt[1:]] or val
            elif opt == '-pix_fmt':
                pass
            else:
                raise _FFError(f'unsupported option {opt} inside -vf')
    o['vf'] = ','.join(keep)


def _ff_probe(path: str, in_opts: Optional[list] = None) -> dict:
    """codec, size, fps, frame count, duration, audio presence of an input file."""
    ffp = find_ffprobe()
    if not ffp or path == '-':
        return {}
    # only demuxer options make sense for ffprobe (e.g. -framerate / -start_number for sequences)
    opts = []
    it = iter(in_opts or [])
    for a in it:
        v = next(it, None)
        if a in ('-f', '-framerate', '-start_number', '-pattern_type', '-r') and v is not None:
            opts += ['-framerate' if a == '-r' else a, v]
    try:
        r = subprocess.run([ffp, '-v', 'error', '-print_format', 'json', '-show_format',
                            '-show_streams'] + opts + [path],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, env=_ff_clean_env())
        d = json.loads(r.stdout or '{}')
    except Exception:
        return {}
    streams = d.get('streams', [])
    vs = next((s for s in streams if s.get('codec_type') == 'video'
               and not (s.get('disposition') or {}).get('attached_pic')), None)
    if not vs:
        return {'has_video': False}
    fps = _ff_rational(vs.get('avg_frame_rate', '0/1')) or _ff_rational(vs.get('r_frame_rate', '0/1'))
    dur = float(vs.get('duration') or d.get('format', {}).get('duration') or 0)
    try:
        nb = int(vs.get('nb_frames') or 0)
    except ValueError:
        nb = 0
    if nb <= 0 and dur > 0 and fps > 0:
        nb = int(round(dur * fps))
    return {'has_video': True, 'codec': vs.get('codec_name', ''),
            'width': vs.get('width', 0), 'height': vs.get('height', 0),
            'tag': vs.get('codec_tag_string', ''), 'fps': fps, 'frames': nb, 'duration': dur,
            'rate_str': next((r for r in (vs.get('r_frame_rate'), vs.get('avg_frame_rate'))
                              if r and 0 < _ff_rational(r) <= 240), ''),
            'has_audio': any(s.get('codec_type') == 'audio' for s in streams)}


def _ff_expected_frames(info: dict, o: dict) -> int:
    total = info.get('frames', 0) or 0
    r = _ff_rational(o['out_rate']) if o['out_rate'] else 0
    if r > 0 and info.get('duration'):
        total = int(round(info['duration'] * r))
    if o.get('ss') or o.get('to') or o.get('t'):
        rate = r or info.get('fps') or 0
        dur = info.get('duration') or 0
        start = _ff_time(o['ss']) if o.get('ss') else 0.0
        end = _ff_time(o['to']) if o.get('to') else (start + _ff_time(o['t']) if o.get('t') else dur)
        if dur > 0:
            end = min(end, dur)
        total = int(round((end - start) * rate)) if rate > 0 and end > start else -1
        if rate > 0 and end > start and not r:
            # ffmpeg's rule (same as pphap 1.1.7): keep a frame if its time is >= IN and its time after
            # IN, rounded to the nearest frame (halves up), is < the length; IN / length to 6 decimals
            s0 = round(start, 6) * rate
            d = round(end - start, 6) * rate
            first = math.ceil(s0 - 1e-9)
            n = int(round(dur * rate)) if dur > 0 else first + int(d) + 2
            last = first
            while last < n and math.floor(last - s0 + 0.5) < d - 1e-9:
                last += 1
            total = last - first if last > first else -1
    if o['frames'] > 0:
        total = min(total, o['frames']) if total > 0 else o['frames']
    return total if total > 0 else -1


class _FFRunner:
    def __init__(self, o: dict):
        self.o = o
        self.proc = None
        self.interrupted = False
        self.tmpdir = None

    # ── signals ──
    def on_signal(self, signum, frame):
        self.interrupted = True
        if getattr(self, 'producer', None) and self.producer.poll() is None:
            try:
                self.producer.kill()
            except Exception:
                pass
        p = self.proc
        if IS_WIN and signum == signal.SIGINT and _has_console():
            return      # Ctrl+C in a console reaches ffmpeg / pphap too; they finish the file themselves
        if p and p.poll() is None:
            try:
                stop_process(p, signal.SIGINT if self.is_ffmpeg else signal.SIGTERM)
            except Exception:
                pass

    def log(self, msg: str, level: str = 'info'):
        order = ['quiet', 'panic', 'fatal', 'error', 'warning', 'info', 'verbose', 'debug', 'trace']
        cur = order.index(self.o['loglevel']) if self.o['loglevel'] in order else 3
        if order.index(level) <= cur:
            sys.stderr.write(f'{msg}\n')
            sys.stderr.flush()

    def child_loglevel(self) -> str:
        lv = self.o['loglevel']
        if lv == 'info':
            return 'warning'        # keep ffmpeg's banner/stream dump for -loglevel verbose
        return lv if lv in ('quiet', 'panic', 'fatal', 'error', 'warning',
                            'verbose', 'debug', 'trace') else 'error'

    # ── ffmpeg run with progress translation ──
    def run_ffmpeg(self, cmd: list, prog: _FFProgress, out_path: str, stdin=None) -> int:
        self.is_ffmpeg = True
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=stdin,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1, env=_ff_clean_env())
        frames = 0; size = 0
        for line in self.proc.stdout:
            k, _, v = line.strip().partition('=')
            if k == 'frame':
                try: frames = int(v)
                except ValueError: pass
            elif k == 'total_size':
                try: size = int(v)
                except ValueError: pass
            elif k == 'progress' and prog is not None:
                prog.emit(frames, size, force=True)
        rc = self.proc.wait()
        self.last_frames = frames
        self.last_size = size or (os.path.getsize(out_path) if os.path.exists(out_path) else 0)
        return rc

    def _prepare_sequence(self) -> bool:
        """Image-sequence input? Expands `-i shot_0001.png` to its numbered sequence."""
        o = self.o
        inp = o['input'] or ''
        if inp == '-':
            return False
        flags = o['in_opts'][::2]
        if '%' not in inp and Path(inp).suffix.lower() in IMAGE_EXTS and os.path.isfile(inp):
            r = detect_sequence(inp)
            if r:
                o['input'], start, count = r
                if '-start_number' not in flags:
                    o['in_opts'] += ['-start_number', str(start)]
                self.seq_count = count
            if '-framerate' not in flags and '-r' not in flags:
                o['in_opts'] += ['-framerate', '30']
            return True
        return '%' in inp or ('-f' in flags and o['in_opts'][flags.index('-f') * 2 + 1] == 'image2')

    def run(self) -> int:
        o = self.o
        self.is_ffmpeg = True
        self.producer = None
        self.seq_count = 0
        is_seq = self._prepare_sequence()
        out = o['output'] or ''
        raw_out = (out in ('-', 'pipe:', 'pipe:1') or o['codec'] == 'rawvideo'
                   or self._extra_value('-f') == 'rawvideo')
        if not o['codec_set'] and not raw_out and out and Path(out).suffix.lower() not in ('.mov', '.qt', ''):
            o['codec'] = ''         # e.g. frame.png, clip.mp4: ffmpeg picks the codec for that file type
        codec = o['codec']
        # 1.5.12: Hap R input. pphap 1.1.7+ decodes it (ffmpeg can't)
        hapr_in = False
        if o['input'] and o['input'] != '-' and not is_seq and os.path.isfile(o['input']):
            hapr_in = _ff_probe(o['input'], o['in_opts']).get('tag') == 'Hap7'
        if hapr_in and not find_hapr_decoder():
            raise _FFError('input is Hap R: reading it needs pphap 1.1.7 or later '
                           '(PixelPress HAP 1.1.7), which this copy of PixelPress does not include')
        if raw_out:
            # decoded frames out (stdout or a file): pphap does it for any input
            if is_seq:
                raise _FFError('raw frame output is not available for image sequences')
            if not find_hapr_decoder():
                raise _FFError('raw frame output needs pphap 1.1.7 or later')
            if o['input'] != '-' and not os.path.exists(o['input'] or ''):
                raise _FFError(f"input not found: {o['input']}")
            return self.exec_pphap(find_pphap())       # does not return
        if hapr_in and codec in FF_HAP_CODECS and not o['extra'] and (o['audio'] or 'pcm').startswith('pcm'):
            return self.exec_pphap(find_pphap())       # Hap R -> Hap: pphap decodes and encodes
        if codec in FF_PPHAP_ONLY or codec == 'hap_r':
            pphap = find_pphap()
            if pphap and not is_seq:
                if o['input'] and o['input'] != '-' and not os.path.exists(o['input']):
                    raise _FFError(f"input not found: {o['input']}")
                return self.exec_pphap(pphap)          # does not return
            if codec != 'hap_r':
                raise _FFError(f"codec '{codec}' needs pphap (the PixelPress HAP plugin's encoder), "
                               f"which isn't bundled with this copy of PixelPress")
        if not o['input']:
            raise _FFError('no input (-i FILE)')
        if not o['output']:
            raise _FFError('no output file')
        inp, out = o['input'], o['output']
        if inp != '-' and not is_seq and not os.path.exists(inp):
            raise _FFError(f'input not found: {inp}')
        if os.path.exists(out):
            if o['no_overwrite']:
                raise _FFError(f'output exists: {out}')
            if not o['overwrite']:
                raise _FFError(f'output exists: {out} (use -y to overwrite)')
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise _FFError('ffmpeg not found')

        info = _ff_probe(inp, o['in_opts'])
        if is_seq and not info.get('has_video'):
            raise _FFError(f'could not read image sequence {inp}')
        if self.seq_count and info.get('fps'):
            info['frames'] = self.seq_count
        if inp != '-' and info and not info.get('has_video', True):
            raise _FFError(f'no video stream in {inp}')

        prog = _FFProgress(o['progress'], o['stats'] and o['loglevel'] not in ('quiet', 'panic', 'fatal'),
                           o['period'])
        try:
            if info.get('tag') == 'Hap7':
                return self.run_hapr_decode(ffmpeg, info, prog)
            if is_seq and (codec in FF_PPHAP_ONLY or codec == 'hap_r') and find_pphap():
                return self.run_pphap_piped(find_pphap(), ffmpeg, info, prog)
            if codec == 'hap_r':
                return self.run_hapr(ffmpeg, info, prog)
            return self.run_ff_encode(ffmpeg, info, prog)
        finally:
            prog.close()
            if self.tmpdir:
                shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _frame_size(self, ffmpeg: str, vf: list, info: dict) -> Tuple[int, int]:
        """Output frame size: probed size, or (with filters) decoded from one PNG frame."""
        o = self.o
        if not vf:
            return int(info.get('width') or 0), int(info.get('height') or 0)
        r = subprocess.run([ffmpeg, '-v', 'error', '-nostdin'] + o['in_opts'] + ['-i', o['input']] + vf +
                           ['-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'],
                           capture_output=True, env=_ff_clean_env())
        b = r.stdout
        if len(b) >= 24 and b[12:16] == b'IHDR':
            return int.from_bytes(b[16:20], 'big'), int.from_bytes(b[20:24], 'big')
        return 0, 0

    def run_pphap_piped(self, pphap: str, ffmpeg: str, info: dict, prog: _FFProgress) -> int:
        """Image sequences → pphap: ffmpeg decodes frames to RGBA and pipes them in."""
        o = self.o
        vf = self._vf_chain(False)
        w, h = self._frame_size(ffmpeg, vf, info)
        if not (w and h):
            raise _FFError(f'could not read image sequence {o["input"]}')
        flags = o['in_opts'][::2]
        rate = o['out_rate']
        for f in ('-framerate', '-r'):
            if not rate and f in flags:
                rate = o['in_opts'][flags.index(f) * 2 + 1]
        rate = rate or (f"{info['fps']:g}" if info.get('fps') else '25')
        prog.rate = _ff_rational(rate) or prog.rate
        prog.total = _ff_expected_frames(info, o)
        src = ([ffmpeg, '-v', 'error', '-nostdin'] + o['in_opts'] + ['-i', o['input']] + vf +
               self._common_out() + ['-f', 'rawvideo', '-pix_fmt', 'rgba', '-'])
        cmd = ([pphap, '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', rate, '-i', '-',
                '-c:v', o['codec']] + o['pphap_opts'] +
               (['-chunks', str(o['chunks'])] if o['chunks'] > 0 else []) +
               ['-y', '-nostats', '-progress', 'pipe:1', o['output']])
        if self.o['loglevel'] in ('quiet', 'panic', 'fatal', 'error'):
            cmd[1:1] = ['-loglevel', 'quiet']
        self.log('PixelPress: ' + quote_cmd(src) + ' | ' + quote_cmd(cmd), 'verbose')
        out_abs = os.path.abspath(o['output'])
        cmd[-1] = out_abs
        if os.path.exists(out_abs):
            os.remove(out_abs)
        self.is_ffmpeg = True                       # pphap finalises the file on SIGINT
        self.producer = subprocess.Popen(src, stdout=subprocess.PIPE, env=_ff_clean_env())
        self.proc = subprocess.Popen(cmd, stdin=self.producer.stdout, stdout=subprocess.PIPE,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1, env=pphap_env(),
                                     cwd=tempfile.gettempdir())   # keeps PPHAP-log.txt out of the way
        self.producer.stdout.close()
        frames = 0; size = 0
        prog.emit(0, 0, force=True)
        for line in self.proc.stdout:
            k, _, v = line.strip().partition('=')
            try:
                if k == 'frame':
                    frames = int(v)
                elif k == 'total_size':
                    size = int(v)
                elif k == 'progress':
                    prog.emit(frames, size)
            except ValueError:
                pass
        rc = self.proc.wait()
        prc = self.producer.wait()
        size = os.path.getsize(out_abs) if os.path.exists(out_abs) else size
        prog.emit(frames, size, end=True)
        if self.interrupted or rc == 255:
            return FF_EXIT_INT
        if rc != 0 or prc not in (0, -9):
            self.log(f'PixelPress: encode failed (pphap exit {rc}, ffmpeg exit {prc})', 'error')
            return FF_EXIT_ERR
        return FF_EXIT_OK

    def _audio_args(self, has_audio: bool) -> list:
        """Like ffmpeg: the source's audio comes along unless -an. Default codec: PCM in .mov
        (what Hap players expect), else ffmpeg's default for the container."""
        o = self.o
        if o['no_audio'] or not has_audio:
            return ['-an']
        if o['audio']:
            return ['-c:a', o['audio']]
        if Path(o['output'] or '').suffix.lower() in ('.mov', '.qt'):
            return ['-c:a', 'pcm_s16le']
        return []

    def _extra_value(self, opt: str) -> str:
        e = self.o['extra']
        for j, a in enumerate(e[:-1]):
            if a == opt:
                return e[j + 1]
        return ''

    def run_hapr_decode(self, ffmpeg: str, info: dict, prog: _FFProgress) -> int:
        """Hap R input -> any ffmpeg codec (ProRes, H.264, PNG, Hap with audio, ...):
        pphap decodes (with in/out points and -vf) and pipes RGBA frames into ffmpeg."""
        o = self.o
        codec = o['codec']
        extra = []
        for k in ('ss', 'to', 't'):
            if o.get(k):
                extra += [f'-{k}', o[k]]
        vf = self._vf_chain(False)
        extra += vf
        if o['frames'] > 0:
            extra += ['-frames:v', str(o['frames'])]
        try:
            producer, w, h, n = start_hapr_decode(o['input'], extra)
        except RuntimeError as e:
            raise _FFError(f'could not decode {o["input"]}: {e}')
        if n <= 0:                                  # Windows fallback: same rule pphap uses
            n = _ff_expected_frames(info, o)
        self.producer = producer
        err: list = []
        t = threading.Thread(target=lambda: err.extend(
            l.decode(errors='replace').rstrip() for l in producer.stderr if l.strip()), daemon=True)
        t.start()
        src_rate = info.get('rate_str') or (f"{info['fps']:g}" if info.get('fps') else '30')
        cmd = [ffmpeg, '-hide_banner', '-y', '-loglevel', self.child_loglevel(),
               '-progress', 'pipe:1', '-nostats', '-stats_period', str(o['period']),
               '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', src_rate, '-i', '-']
        audio_args = self._audio_args(bool(info.get('has_audio')))
        if audio_args != ['-an']:                   # audio straight from the source, same range
            # cut to start on the first frame pphap delivers and to last as long as the video (exact sync)
            a_in = []
            fps = _ff_rational(src_rate)
            if o.get('ss') and fps > 0:
                first_t = math.ceil(round(_ff_time(o['ss']), 6) * fps - 1e-9) / fps
                a_in += ['-ss', f'{first_t:.6f}']
            if n > 0 and fps > 0:
                a_in += ['-t', f'{n / fps:.6f}']
            cmd += a_in + ['-i', o['input'], '-map', '0:v', '-map', '1:a?']
        if o['out_rate']:
            cmd += ['-r', o['out_rate']]
        if o['threads'] > 0:
            cmd += ['-threads', str(o['threads'])]
        out_ext = Path(o['output']).suffix.lower()
        if codec in FF_HAP_CODECS:                 # only reached with audio or extra ffmpeg options
            cmd += ['-vf', 'pad=ceil(iw/4)*4:ceil(ih/4)*4', '-c:v', 'hap', '-format', codec]
            if o['chunks'] > 0:
                cmd += ['-chunks', str(o['chunks'])]
        else:
            if codec:
                cmd += ['-c:v', codec]
            rgb_out = (codec in FF_RGB_CODECS or (not codec and out_ext in FF_RGB_EXTS)
                       or (codec == 'prores_ks' and self._extra_value('-pix_fmt').startswith(('rgb', 'gbr'))))
            if not rgb_out and '-colorspace' not in o['extra'] and '-vf' not in o['extra']:
                # RGB -> YUV with the HD matrix, tagged, so players show the colours Hap R holds
                cmd += ['-vf', 'scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int',
                        '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                        '-color_range', 'tv']
                if out_ext in ('.mov', '.mp4', '.m4v', '.qt') and '-movflags' not in o['extra']:
                    cmd += ['-movflags', '+write_colr']
        cmd += o['extra']
        cmd += audio_args
        cmd.append(o['output'])
        self.log('PixelPress: pphap -i ' + quote_arg(o['input']) + ' ' + quote_cmd(extra) +
                 ' -f rawvideo -pix_fmt rgba - | ' + quote_cmd(cmd), 'verbose')
        prog.rate = _ff_rational(o['out_rate']) or _ff_rational(src_rate) or prog.rate
        prog.total = n if n > 0 else _ff_expected_frames(info, o)
        if o['out_rate'] and n > 0 and _ff_rational(src_rate) > 0:
            prog.total = int(round(n * _ff_rational(o['out_rate']) / _ff_rational(src_rate)))
        prog.emit(0, 0, force=True)
        try:
            rc = self.run_ffmpeg(cmd, prog, o['output'], stdin=producer.stdout)
        finally:
            producer.stdout.close()                 # ffmpeg gone: pphap stops on the closed pipe
            try:
                prc = producer.wait(timeout=15)
            except subprocess.TimeoutExpired:
                producer.kill(); prc = producer.wait()
            t.join(timeout=2)
        prog.emit(self.last_frames, self.last_size, end=True)
        if self.interrupted:
            return FF_EXIT_INT
        if prc not in (0, -9, 255):
            self.log(f'PixelPress: Hap R decode failed: {err[-1] if err else f"pphap exit {prc}"}', 'error')
            return FF_EXIT_ERR
        if rc != 0:
            self.log(f'PixelPress: ffmpeg failed (exit {rc})', 'error')
            return FF_EXIT_ERR
        return FF_EXIT_OK

    def exec_pphap(self, pphap: str):
        """Hand the command over to pphap: same options, progress keys and exit codes."""
        argv = self.raw_argv
        out = []; i = 0; seen_input = False; scale = None
        # 1.5.12: -an / -c:a go to pphap 1.1.7, which carries the source's audio (as PCM) like ffmpeg
        old_pphap = pphap_version(pphap) < (1, 1, 7)
        while i < len(argv):
            a = argv[i]
            if old_pphap and a == '-an':
                i += 1; continue
            if old_pphap and a in ('-c:a', '-acodec', '-codec:a'):
                i += 2; continue
            if a == '-i':
                seen_input = True
            if a in ('-vf', '-filter:v') and i + 1 < len(argv):
                out += [a, _vf_add_font(argv[i + 1])]; i += 2; continue
            if a in ('-s', '-video_size') and seen_input and i + 1 < len(argv):
                w, _, h = argv[i + 1].partition('x'); scale = f'scale={w}:{h}'; i += 2; continue
            out.append(a); i += 1
        if scale:
            for j, a in enumerate(out):
                if a in ('-vf', '-filter:v') and j + 1 < len(out):
                    out[j + 1] += ',' + scale; break
            else:
                out[-1:-1] = ['-vf', scale]
        sys.stdout.flush(); sys.stderr.flush()
        if IS_WIN:
            # Windows has no exec: os.execve would start pphap and end this process at once, so the
            # caller would lose the progress output and exit code. Run it and pass both through.
            def fd(f, default):
                try:
                    return f.fileno()
                except Exception:
                    return default
            p = subprocess.Popen([pphap] + out, env=pphap_env(), stdin=fd(sys.stdin, subprocess.DEVNULL),
                                 stdout=fd(sys.stdout, None), stderr=fd(sys.stderr, None))
            self.proc, self.is_ffmpeg = p, False
            try:
                return p.wait()
            except KeyboardInterrupt:
                return p.wait()
        os.execve(pphap, [pphap] + out, pphap_env())

    def _vf_chain(self, pad4: bool) -> list:
        o = self.o
        f = []
        if o['vf']:
            f.append(o['vf'])
        if o['out_size']:
            w, _, h = o['out_size'].partition('x')
            f.append(f'scale={w}:{h}')
        if pad4:
            f.append('pad=ceil(iw/4)*4:ceil(ih/4)*4')
        return ['-vf', ','.join(f)] if f else []

    def _common_in(self) -> list:
        o = self.o
        a = []
        if o['input'] != '-':
            a.append('-nostdin')
        if o.get('ss'):
            a += ['-ss', f"{_ff_time(o['ss']):.6f}"]       # before -i: fast and frame-accurate
        return a + o['in_opts'] + ['-i', o['input']]

    def _common_out(self) -> list:
        o = self.o
        a = []
        if o['out_rate']:
            a += ['-r', o['out_rate']]
        if o.get('to') or o.get('t'):
            start = _ff_time(o['ss']) if o.get('ss') else 0.0
            dur = (_ff_time(o['to']) - start) if o.get('to') else _ff_time(o['t'])
            a += ['-t', f'{dur:.6f}']
        if o['frames'] > 0:
            a += ['-frames:v', str(o['frames'])]
        if o['threads'] > 0:
            a += ['-threads', str(o['threads'])]
        return a

    def run_ff_encode(self, ffmpeg: str, info: dict, prog: _FFProgress) -> int:
        o = self.o
        is_hap = o['codec'] in FF_HAP_CODECS
        prog.rate = _ff_rational(o['out_rate']) or info.get('fps') or prog.rate
        prog.total = _ff_expected_frames(info, o)
        cmd = [ffmpeg, '-hide_banner', '-y', '-loglevel', self.child_loglevel(),
               '-progress', 'pipe:1', '-nostats', '-stats_period', str(o['period'])]
        cmd += self._common_in() + self._vf_chain(is_hap) + self._common_out()
        if is_hap:
            cmd += ['-c:v', 'hap', '-format', o['codec']]
            if o['chunks'] > 0:
                cmd += ['-chunks', str(o['chunks'])]
        elif o['codec']:
            cmd += ['-c:v', o['codec']]
        cmd += o['extra']
        cmd += self._audio_args(bool(info.get('has_audio')) and o['input'] != '-')
        cmd.append(o['output'])
        self.log('PixelPress: ' + quote_cmd(cmd), 'verbose')
        prog.emit(0, 0, force=True)
        rc = self.run_ffmpeg(cmd, prog, o['output'], stdin=sys.stdin if o['input'] == '-' else None)
        prog.emit(self.last_frames, self.last_size, end=True)
        if self.interrupted:
            return FF_EXIT_INT
        if rc != 0:
            self.log(f'PixelPress: ffmpeg failed (exit {rc})', 'error')
            return FF_EXIT_ERR
        return FF_EXIT_OK

    def run_hapr(self, ffmpeg: str, info: dict, prog: _FFProgress) -> int:
        o = self.o
        hapr = find_legacy_hapr()
        if not hapr:
            raise _FFError('no Hap R encoder found (pphap / hapr_enc missing from the app)')
        src = o['input']
        needs_prepass = (src == '-' or o['in_opts'] or o['vf'] or o['out_rate'] or o['out_size']
                         or o.get('ss') or o.get('to') or o.get('t')
                         or o['frames'] > 0 or Path(src).suffix.lower() not in FF_AV_EXTS
                         or (info.get('codec') or '') not in FF_AV_CODECS)
        if needs_prepass:
            # Convert to a ProRes 4444 (+alpha) intermediate hapr_enc can read.
            self.tmpdir = tempfile.mkdtemp(prefix='pixelpress_')
            tmp = os.path.join(self.tmpdir, 'intermediate.mov')
            self.log('PixelPress: preparing source for Hap R …', 'info')
            cmd = [ffmpeg, '-hide_banner', '-y', '-loglevel', self.child_loglevel(),
                   '-progress', 'pipe:1', '-nostats', '-stats_period', str(o['period'])]
            cmd += self._common_in() + self._vf_chain(False) + self._common_out()
            cmd += ['-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p10le',
                    '-c:a', 'pcm_s16le', tmp]
            pre_total = _ff_expected_frames(info, o)
            prog.total = pre_total
            prog.rate = _ff_rational(o['out_rate']) or info.get('fps') or prog.rate
            prog.emit(0, 0, force=True)
            self.proc = None
            # frames during the pre-pass are not output frames yet: report 0
            rc = self.run_ffmpeg(cmd, None, tmp, stdin=sys.stdin if src == '-' else None)
            if self.interrupted:
                prog.emit(0, 0, end=True)
                return FF_EXIT_INT
            if rc != 0:
                self.log(f'PixelPress: could not read the input (ffmpeg exit {rc})', 'error')
                prog.emit(0, 0, end=True)
                return FF_EXIT_ERR
            src = tmp
            info = _ff_probe(tmp)

        prog.rate = info.get('fps') or prog.rate
        prog.total = info.get('frames') or -1
        prog.start = time.monotonic()   # fps/speed measure the Hap R encode itself
        out = o['output']
        if os.path.exists(out):
            os.remove(out)          # -y already checked; avoids hapr_enc's overwrite dialog
        self.is_ffmpeg = False
        self.proc = subprocess.Popen([hapr, src, out], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1,
                                     env=_ff_clean_env())
        err_lines = []

        def pump_err():
            for line in self.proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                err_lines.append(line)
                if line.startswith('ERROR'):
                    self.log(f'hapr_enc: {line[5:].strip()}', 'error')
                else:
                    self.log(f'hapr_enc: {line[5:].strip()}', 'verbose')

        t = threading.Thread(target=pump_err, daemon=True)
        t.start()
        prog_re = re.compile(r'PROGRESS\s+([\d.]+)(.*)')
        frames_re = re.compile(r'frames=(\d+)')
        done_re = re.compile(r'Done\D+(\d+)\s+frames')
        frames = 0
        prog.emit(0, 0, force=True)
        for line in self.proc.stdout:
            m = prog_re.match(line.strip())
            if not m:
                continue
            pct = float(m.group(1)); rest = m.group(2)
            fm = frames_re.search(rest); dm = done_re.search(rest)
            if dm:
                frames = int(dm.group(1))
                if prog.total <= 0 or abs(prog.total - frames) <= 2:
                    prog.total = frames
            elif fm:
                frames = int(fm.group(1))
            elif prog.total > 0:
                frames = int(round(pct * prog.total))
            size = os.path.getsize(out) if os.path.exists(out) else 0
            prog.emit(frames, size)
        rc = self.proc.wait()
        t.join(timeout=2)
        size = os.path.getsize(out) if os.path.exists(out) else 0
        prog.emit(frames, size, end=True)
        if self.interrupted:
            return FF_EXIT_INT
        if rc != 0:
            if not any(l.startswith('ERROR') for l in err_lines):
                self.log(f'PixelPress: Hap R encode failed (exit {rc})', 'error')
            return FF_EXIT_ERR
        return FF_EXIT_OK


def ff_cli(argv: list) -> int:
    if sys.stdout is None:          # windowed builds can start without std streams
        sys.stdout = open(1, 'w', closefd=False)
    if sys.stderr is None:
        sys.stderr = open(2, 'w', closefd=False)
    for st in (sys.stdout, sys.stderr):     # a path the console code page can't show must not crash a job
        try:
            st.reconfigure(errors='replace')
        except Exception:
            pass
    try:
        o = _ff_parse(argv)
    except (_FFError, ValueError) as e:
        sys.stderr.write(f'PixelPress: {e}\n')
        return FF_EXIT_ERR
    if o.get('help'):
        sys.stdout.write(FF_USAGE.format(v=VERSION)); sys.stdout.flush()
        return FF_EXIT_OK
    if o.get('version'):
        sys.stdout.write(f'pixelpress {VERSION}\n'); sys.stdout.flush()
        return FF_EXIT_OK
    if o.get('licenses'):
        path = find_licenses_file()
        if not path:
            sys.stderr.write('PixelPress: licence file not found\n'); return FF_EXIT_ERR
        try:
            sys.stdout.write(Path(path).read_text(encoding='utf-8')); sys.stdout.flush()
        except BrokenPipeError:          # reader stopped early (e.g. | head, | grep -q)
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return FF_EXIT_OK
    runner = _FFRunner(o)
    runner.raw_argv = list(argv)
    for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, 'SIGHUP', None)):
        if s is not None:
            signal.signal(s, runner.on_signal)
    try:
        return runner.run()
    except _FFError as e:
        sys.stderr.write(f'PixelPress: {e}\n')
        return FF_EXIT_ERR
    except BrokenPipeError:
        runner.on_signal(None, None)
        return FF_EXIT_INT


def _is_ff_cli(argv: list) -> bool:
    args = [a for a in argv if not a.startswith('-psn_')]   # Finder launch arg
    return any(a in ('-i', '-h', '-help', '--help', '-version', '--version',
                     '-L', '-licenses', '--licenses') for a in args)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if '--cli' in sys.argv:
        sys.argv.remove('--cli')
        cli_run()
    elif _is_ff_cli(sys.argv[1:]):
        sys.exit(ff_cli([a for a in sys.argv[1:] if not a.startswith('-psn_')]))
    elif not _GUI_AVAILABLE:
        print('customtkinter not installed. Use --cli mode.', file=sys.stderr)
        sys.exit(1)
    else:
        ctk.set_appearance_mode('dark')
        ctk.set_default_color_theme('blue')
        app = PixelPressApp()
        app.protocol('WM_DELETE_WINDOW', app.on_closing)
        app.mainloop()
