# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PixelPress - one spec for both platforms:
#   macOS:   PixelPress.app            (Make Installer.command)
#   Windows: PixelPress\PixelPress.exe  windowed app
#            PixelPress\PixelPress.com  the same program as a console command (pixelpress -i ... in a
#                                       terminal; Windows runs .com before .exe for "pixelpress")
#            (Make Installer (Windows).bat / make_installer_windows.py)
import os, re, sys
from PyInstaller.utils.hooks import collect_data_files

WIN   = sys.platform == 'win32'
_VER  = re.search(r"^VERSION = '([^']+)'", open(os.path.join(SPECPATH, 'pixelpress.py'), encoding='utf-8').read(), re.M).group(1)
_root = os.path.dirname(SPECPATH)               # Pixel Press Standalone/
_tp   = os.path.join(_root, '3rd Party')
_tpw  = os.path.join(_tp, 'Windows')             # Windows: ffmpeg.exe, ffprobe.exe, pphap.exe + its VC++ DLLs

if WIN:
    _bins = [(os.path.join(_tpw, f), '.') for f in sorted(os.listdir(_tpw))
             if f.lower().endswith(('.exe', '.dll'))]
    _ctk  = collect_data_files('customtkinter')
else:
    _bins = [(os.path.join(_root, 'Deployment', 'hapr_enc'), '.')]
    for _b in ('ffmpeg', 'ffprobe', 'pphap'):  # portable ffmpeg (Make Installer.command) + plugin encoder
        if os.path.isfile(os.path.join(_tp, _b)):
            _bins.append((os.path.join(_tp, _b), '.'))
    _ctk  = [('/opt/homebrew/lib/python3.14/site-packages/customtkinter', 'customtkinter')]

# drag and drop (tkdnd native library + Tcl scripts, from the tkinterdnd2 package)
_dnd_datas = collect_data_files('tkinterdnd2')

a = Analysis(
    [os.path.join(SPECPATH, 'pixelpress.py')],
    pathex=[],
    binaries=_bins,
    datas=_ctk
          + _dnd_datas
          + ([(os.path.join(_tp, 'pphap-licenses.txt'), '.')]
             if os.path.isfile(os.path.join(_tp, 'pphap-licenses.txt')) else [])
          + [(os.path.join(SPECPATH, 'licenses', _f), '.')
             for _f in ('THIRD-PARTY-LICENSES.txt', 'LICENSE.txt')
             if os.path.isfile(os.path.join(SPECPATH, 'licenses', _f))],
    hiddenimports=['tkinterdnd2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if WIN:
    _vinfo = os.path.join(SPECPATH, 'build', 'version_info.txt')   # written by make_installer_windows.py
    _common = dict(debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
                   disable_windowed_traceback=False, target_arch=None,
                   icon=os.path.join(SPECPATH, 'AppIcon.ico'),
                   version=_vinfo if os.path.isfile(_vinfo) else None)
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='PixelPress', console=False, **_common)
    cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name='PixelPress-cli', console=True, **_common)
    coll = COLLECT(exe, cli, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name='PixelPress')
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='PixelPress',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=[os.path.join(SPECPATH, 'AppIcon.icns')],
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='PixelPress',
    )
    app = BUNDLE(
        coll,
        name='PixelPress.app',
        icon=os.path.join(SPECPATH, 'AppIcon.icns'),
        bundle_identifier='com.pixelpress.app',
        info_plist={
            'CFBundleShortVersionString': _VER,
            'CFBundleVersion': _VER,
            'NSHighResolutionCapable': True,
        },
    )
