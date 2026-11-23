@echo off
rem PixelPress - builds the Windows installer (see make_installer_windows.py).
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 make_installer_windows.py %*) else (python make_installer_windows.py %*)
