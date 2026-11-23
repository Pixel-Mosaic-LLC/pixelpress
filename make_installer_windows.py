#!/usr/bin/env python3
"""
PixelPress - Windows installer build (the Windows twin of Make Installer.command).
Run it with "Make Installer (Windows).bat" (double-click), or:   py make_installer_windows.py [1.6.0]

Builds PixelPress.exe (windowed) + PixelPress.com (console: "pixelpress -i ..." in a terminal) with
ffmpeg.exe / ffprobe.exe / pphap.exe bundled, tests the command line, then makes:
    Deployment/Releases/<version>/PixelPress-<version>-Windows-x64.exe          installer
    Deployment/Releases/<version>/PixelPress-<version>-Windows-x64-portable.zip  the app folder, no installer
    ...plus the licence files and the source packages that every public release needs (LGPL):
    PixelPress-<version>-windows-ffmpeg-source.zip and the PixelPress HAP (pphap) source zips.

Version: like the Mac script, every build is a NEW version: if this version already has a release,
the patch number is bumped in pixelpress.py first. The one exception: a release made by the Mac
script from exactly the same pixelpress.py (Releases/<v>/pixelpress.py.md5) gets the Windows
installer added to it, so both platforms of one source share a version. Pass a version to jump to it.

Needs (one-time):
    Python 3.12+ from python.org (winget install Python.Python.3.12) - the script installs
        pyinstaller, customtkinter and tkinterdnd2 into it when missing
    Inno Setup 6 (winget install JRSoftware.InnoSetup)
    3rd Party/Windows/: ffmpeg.exe, ffprobe.exe (LGPL, from build_ffmpeg_lgpl_win.sh, with its source/
        folder), pphap.exe + its Visual C++ DLLs (copy C:\\Program Files\\PixelPress\\*.exe/*.dll after
        installing the PixelPress HAP plugin, minus ffmpeg/ffprobe), and 3rd Party/pphap-licenses.txt
"""
import os, re, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent                    # Source Code/
ROOT = HERE.parent                                        # Pixel Press Standalone/
TP = ROOT / '3rd Party'; TPW = TP / 'Windows'
DEPLOY = ROOT / 'Deployment'; RELEASES = DEPLOY / 'Releases'
SRC = HERE / 'pixelpress.py'
IS_WIN = sys.platform == 'win32'
NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

def say(m): print(m, flush=True)

def fail(m, old=None, new=None):
    say(f'\nERROR: {m}')
    if old and new and old != new:
        set_version(old); say(f'  (version reverted to {old})')
    if os.environ.get('PIXELPRESS_NO_PAUSE') != '1':
        try: input('Press Enter to close...')
        except EOFError: pass
    sys.exit(1)

def get_version() -> str:
    return re.search(r"^VERSION = '([^']+)'", SRC.read_text(encoding='utf-8'), re.M).group(1)

def set_version(v: str):
    s = SRC.read_text(encoding='utf-8')
    s = re.sub(r"^VERSION = '[^']*'", f"VERSION = '{v}'", s, count=1, flags=re.M)
    s = re.sub(r'^PixelPress v[0-9][0-9.]*', f'PixelPress v{v}', s, count=1, flags=re.M)
    SRC.write_text(s, encoding='utf-8', newline='\n')

def bump(v: str) -> str:
    a, b, c = (int(x) for x in v.split('.'))
    return f'{a}.{b}.{c + 1}'

def installer_path(v: str) -> Path:
    return RELEASES / v / f'PixelPress-{v}-Windows-x64.exe'

def source_md5() -> str:
    import hashlib
    return hashlib.md5(SRC.read_bytes().replace(b'\r\n', b'\n')).hexdigest()

def version_taken(v: str) -> bool:
    """A version is free for this build if it has no release yet, or if its release was made from
    exactly this pixelpress.py (the Mac build of the same source) and has no Windows installer."""
    d = RELEASES / v
    if not d.exists():
        return False
    if installer_path(v).exists():
        return True
    mark = d / 'pixelpress.py.md5'
    if not mark.is_file():
        return True
    cur = SRC.read_text(encoding='utf-8')
    set_version(v)
    same = mark.read_text().split()[0] == source_md5()
    SRC.write_text(cur, encoding='utf-8', newline='\n')
    return not same

def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', creationflags=NOWIN, **kw)

def main():
    old = get_version()
    new = sys.argv[1] if len(sys.argv) > 1 else old
    if len(sys.argv) <= 1:
        while version_taken(new):
            new = bump(new)
    if new != old:
        set_version(new); say(f'Version bumped: {old} -> {new}')
    F = lambda m: fail(m, old, new)
    say(f'== PixelPress {new} Windows installer build ==')

    # 1. Python packages
    py = sys.executable
    for mod, pkg in (('PyInstaller', 'pyinstaller'), ('customtkinter', 'customtkinter'), ('tkinterdnd2', 'tkinterdnd2')):
        if run([py, '-c', f'import {mod}']).returncode:
            say(f'Installing {pkg} ...')
            if run([py, '-m', 'pip', 'install', '--quiet', pkg]).returncode:
                F(f'could not install {pkg} ({py} -m pip install {pkg})')

    # 2. ffmpeg / ffprobe: LGPL build with Hap and drawtext
    ff, ffp = TPW / 'ffmpeg.exe', TPW / 'ffprobe.exe'
    if not (ff.is_file() and ffp.is_file()):
        F(f'{ff} / ffprobe.exe missing - build them with build_ffmpeg_lgpl_win.sh (Linux / WSL)')
    lic = run([ff, '-hide_banner', '-L']).stdout
    conf = run([ff, '-hide_banner', '-buildconf']).stdout
    if 'GNU Lesser General Public' not in lic or re.search(r'--enable-(gpl|nonfree)', conf):
        F('3rd Party/Windows/ffmpeg.exe is not the LGPL build')
    if ' hap ' not in run([ff, '-hide_banner', '-encoders']).stdout:
        F('bundled ffmpeg.exe has no Hap encoder')
    if 'drawtext' not in run([ff, '-hide_banner', '-filters']).stdout:
        F('bundled ffmpeg.exe has no drawtext filter (timecode burn-in) - use build_ffmpeg_lgpl_win.sh')
    ffsrc = TPW / 'source'
    if not any(ffsrc.glob('FFmpeg-*')):
        F('3rd Party/Windows/source/ (the ffmpeg source archives from build_ffmpeg_lgpl_win.sh) is missing')
    say('OK  LGPL ffmpeg.exe with Hap and drawtext: ' + run([ff, '-hide_banner', '-version']).stdout.split('\n')[0])

    # 3. pphap.exe (Hap R, same encoder as the plugin) + its Visual C++ runtime
    pph = TPW / 'pphap.exe'
    if not pph.is_file():
        F('3rd Party/Windows/pphap.exe missing - copy it (and its *.dll) from C:\\Program Files\\PixelPress')
    for dll in ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll'):
        if not (TPW / dll).is_file():
            F(f'3rd Party/Windows/{dll} missing - copy the *.dll files next to pphap.exe from C:\\Program Files\\PixelPress')
    m = re.search(r'pphap (\d+\.\d+\.\d+)', run([pph, '-version']).stdout)
    if not m:
        F("3rd Party/Windows/pphap.exe doesn't run")
    ppv = m.group(1)
    if not (TP / 'pphap-licenses.txt').is_file():
        F('3rd Party/pphap-licenses.txt missing (the plugin licence.txt)')
    say(f'OK  pphap {ppv} bundled for Hap R')
    # plugin source zips: Pixel Press Plugin/Deployment/Releases/<v>/ (2026-10-30 layout), or older places
    plugin_zips = []
    for plugin_src in (ROOT.parent / 'Pixel Press Plugin' / 'Deployment' / 'Releases' / ppv,
                       ROOT.parent / 'Pixel Press Plugin' / 'Deployment' / 'Releases' / ppv / 'Public source (ship with installers)',
                       ROOT.parent / 'Pixel Press Plugin' / 'Releases' / ppv / 'Public source (ship with installers)'):
        plugin_zips = sorted(plugin_src.glob('*-source.zip')) if plugin_src.is_dir() else []
        if plugin_zips:
            break
    public_ok = bool(plugin_zips)
    if not public_ok:
        say(f'WARNING: no published source zips for pphap {ppv} - this build is for internal use only.')

    # 4. licence files
    for f in ('THIRD-PARTY-LICENSES.txt', 'LICENSE.txt'):
        if not (HERE / 'licenses' / f).is_file():
            F(f'Source Code/licenses/{f} missing')

    # 5. the app (PyInstaller)
    say('Building PixelPress (PyInstaller) ...')
    (HERE / 'build').mkdir(exist_ok=True)
    nums = tuple(int(x) for x in new.split('.')) + (0,)
    (HERE / 'build' / 'version_info.txt').write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Pixel Mosaic'), StringStruct('FileDescription', 'PixelPress - Hap encoder'),
    StringStruct('FileVersion', '{new}'), StringStruct('ProductName', 'PixelPress'),
    StringStruct('ProductVersion', '{new}'), StringStruct('LegalCopyright', 'Pixel Mosaic - MIT License')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
''', encoding='utf-8')
    dist, work = DEPLOY / 'dist-win', DEPLOY / 'build-win'
    r = run([py, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', dist, '--workpath', work,
             HERE / 'PixelPress.spec'], cwd=HERE)
    app = dist / 'PixelPress'
    if r.returncode or not (app / 'PixelPress.exe').is_file():
        say(r.stdout[-3000:] + r.stderr[-3000:]); F('PyInstaller build failed')
    # the console build answers to "pixelpress" in a terminal (.com runs before .exe)
    cli = app / 'PixelPress-cli.exe'
    if cli.is_file():
        shutil.move(str(cli), str(app / 'PixelPress.com'))
    internal = app / '_internal'
    # pphap.exe needs the Visual C++ runtime it was built with (or newer): use its DLLs, not Python's older ones
    for dll in TPW.glob('*.dll'):
        shutil.copy2(dll, internal / dll.name)
    for f in ('ffmpeg.exe', 'ffprobe.exe', 'pphap.exe', 'THIRD-PARTY-LICENSES.txt'):
        if not (internal / f).is_file():
            F(f"{f} didn't make it into the app")
    if not list(internal.glob('tkinterdnd2/tkdnd/win-x64*/*.dll')):
        F("drag and drop library (tkdnd) didn't make it into the app")

    # 6. command-line self-test (the ffmpeg-style CLI Smart Preso etc. use)
    com = app / 'PixelPress.com'
    v = run([com, '-version']).stdout
    if f'pixelpress {new}' not in v:
        F(f'CLI -version check failed: {v!r}')
    ct = Path(tempfile.mkdtemp())
    cin = ct / 'in.mov'
    if run([internal / 'ffmpeg.exe', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=30',
            '-t', '1', '-c:v', 'prores', cin]).returncode:
        F('test clip could not be made')
    for c in ('hap_q', 'hap_r'):
        r = run([com, '-i', cin, '-c:v', c, '-y', '-nostats', '-progress', 'pipe:1', ct / f'{c}.mov'], cwd=ct)
        if r.returncode or 'progress=end' not in r.stdout:
            say(r.stderr[-2000:]); F(f'CLI {c} encode failed')
    r = run([com, '-i', cin, '-vf', 'drawtext=text=TC', '-c:v', 'hap', '-y', '-nostats', ct / 'tc.mov'], cwd=ct)
    if r.returncode:
        say(r.stderr[-2000:]); F('text burn-in (drawtext) does not work with the bundled ffmpeg')
    if 'PIXELPRESS - LICENCES' not in run([com, '-L']).stdout:
        F("CLI -L doesn't print the licences")
    shutil.rmtree(ct, ignore_errors=True)
    say('OK  command line (hap_q, hap_r, -progress pipe:1, burn-in, -L) works')

    # 7. installer (Inno Setup)
    iscc = shutil.which('ISCC') or next((str(p) for p in (
        Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Inno Setup 6' / 'ISCC.exe',
        Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Inno Setup 6' / 'ISCC.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Inno Setup 6' / 'ISCC.exe') if p.is_file()), None)
    if not iscc:
        F('Inno Setup 6 not found (winget install JRSoftware.InnoSetup)')
    out = RELEASES / new
    out.mkdir(parents=True, exist_ok=True)
    say('Packaging installer ...')
    r = run([iscc, '/Q', f'/DAppVersion={new}', f'/DSourceDir={app}', f'/DOutputDir={out}',
             f'/DLicenceDir={HERE / "licenses"}', f'/DPphapLicence={TP / "pphap-licenses.txt"}',
             HERE / 'PixelPress.iss'], cwd=HERE)
    if r.returncode or not installer_path(new).is_file():
        say(r.stdout[-3000:] + r.stderr[-3000:]); F('Inno Setup failed')

    # 8. licences, source packages, portable zip
    for f in ('THIRD-PARTY-LICENSES.txt', 'LICENSE.txt'):
        shutil.copy2(HERE / 'licenses' / f, out / f)
    (out / 'pixelpress.py.md5').write_text(source_md5() + '  pixelpress.py\n')
    with zipfile.ZipFile(out / f'PixelPress-{new}-windows-ffmpeg-source.zip', 'w', zipfile.ZIP_STORED) as z:
        for p in sorted(ffsrc.iterdir()):
            z.write(p, f'PixelPress-{new}-windows-ffmpeg-source/{p.name}')
        bi = TPW / 'build-info.txt'
        if bi.is_file():
            z.write(bi, f'PixelPress-{new}-windows-ffmpeg-source/build-info.txt')
    if public_ok:
        for zp in plugin_zips:
            shutil.copy2(zp, out / zp.name)
    else:
        (out / 'NOT-FOR-PUBLIC-RELEASE-Windows.txt').write_text(
            'INTERNAL BUILD - NOT FOR PUBLIC RELEASE\n\n'
            f'The bundled pphap ({ppv}) has no published source package in Pixel Press Plugin/Deployment/Releases/{ppv}/.\n'
            'Add it (scripts/make-source-release.sh in the plugin project) and rebuild.\n', encoding='utf-8')
    with zipfile.ZipFile(out / f'PixelPress-{new}-Windows-x64-portable.zip', 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(app.rglob('*')):
            if p.is_file():
                z.write(p, Path('PixelPress') / p.relative_to(app))

    say('')
    if not public_ok:
        say('WARNING: internal build only - see NOT-FOR-PUBLIC-RELEASE-Windows.txt')
    say(f'OK  PixelPress {new} for Windows done: {out}')
    for p in sorted(out.iterdir()):
        say(f'    {p.name}  ({p.stat().st_size // 1024:,} KB)' if p.is_file() else f'    {p.name}/')
    if IS_WIN and os.environ.get('PIXELPRESS_NO_PAUSE') != '1':
        os.startfile(str(out))
        try: input('Press Enter to close...')
        except EOFError: pass

if __name__ == '__main__':
    main()
