@echo off
echo ========================================
echo  DS916 Theme Builder - Build Script
echo ========================================
echo.

:: Install dependencies
echo [1/5] Installing Python dependencies...
pip install pyinstaller pillow pyserial pystray --quiet
if errorlevel 1 (echo ERROR: pip install failed && pause && exit /b 1)
echo Done.
echo.

set "INSTALL_DIR=%APPDATA%\DS916Tray"

:: Close any currently-running instance so the exe isn't locked when we try
:: to overwrite it below -- avoids the old uninstall/rebuild/reinstall dance
:: by letting this script handle the "close the old one" step itself.
echo [2/5] Closing any running DS916Tray instance...
tasklist /FI "IMAGENAME eq DS916Tray.exe" 2>nul | find /I "DS916Tray.exe" >nul
if not errorlevel 1 (
  taskkill /IM DS916Tray.exe /F >nul 2>&1
  timeout /t 1 /nobreak >nul
  echo   Closed running instance.
) else (
  echo   Not currently running.
)
echo.

:: Build exe
echo [3/5] Building DS916Tray.exe...
python -m PyInstaller --onefile --windowed --name=DS916Tray --hidden-import=pystray._win32 --hidden-import=PIL._tkinter_finder ds916_tray.py
if errorlevel 1 (echo ERROR: PyInstaller failed && pause && exit /b 1)
echo Done.
echo.

:: Install into AppData -- keeps everything (exe, theme builder, config,
:: themes, discovered sensors) together in one place for easy cleanup later,
:: rather than scattering files across wherever the user happened to
:: extract/build them
echo [4/5] Installing to %%APPDATA%%\DS916Tray...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /Y dist\DS916Tray.exe "%INSTALL_DIR%\DS916Tray.exe" >nul
copy /Y theme_builder.html "%INSTALL_DIR%\theme_builder.html" >nul
echo Done.
echo.

:: Create Desktop and Start Menu shortcuts only if they don't already exist
:: -- on every rebuild after the first, this step is unnecessary work and
:: just adds time to the loop.
echo [5/5] Checking shortcuts...
set "DESKTOP_LNK=%USERPROFILE%\Desktop\DS916 Tray.lnk"
if exist "%DESKTOP_LNK%" (
  echo   Shortcuts already exist, skipping.
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws = New-Object -ComObject WScript.Shell;" ^
    "$desktop = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\DS916 Tray.lnk');" ^
    "$desktop.TargetPath = '%INSTALL_DIR%\DS916Tray.exe';" ^
    "$desktop.WorkingDirectory = '%INSTALL_DIR%';" ^
    "$desktop.IconLocation = '%INSTALL_DIR%\DS916Tray.exe';" ^
    "$desktop.Save();" ^
    "$startMenuDir = [Environment]::GetFolderPath('StartMenu') + '\Programs';" ^
    "$startMenu = $ws.CreateShortcut($startMenuDir + '\DS916 Tray.lnk');" ^
    "$startMenu.TargetPath = '%INSTALL_DIR%\DS916Tray.exe';" ^
    "$startMenu.WorkingDirectory = '%INSTALL_DIR%';" ^
    "$startMenu.IconLocation = '%INSTALL_DIR%\DS916Tray.exe';" ^
    "$startMenu.Save();"
  if errorlevel 1 (
    echo   WARNING: Shortcut creation failed - you can still run DS916Tray.exe directly from:
    echo     %INSTALL_DIR%
  ) else (
    echo   Created.
  )
)
echo.

echo ========================================
echo  Build complete!
echo ========================================
echo.
echo  Installed to: %INSTALL_DIR%
echo    - DS916Tray.exe
echo    - theme_builder.html
echo.

:: Offer to relaunch immediately -- the whole point of this script handling
:: close+rebuild+install in one pass is to get back to a running app fast.
set /p RELAUNCH="Launch DS916 Tray now? (Y/N): "
if /I "%RELAUNCH%"=="Y" (
  start "" "%INSTALL_DIR%\DS916Tray.exe"
  echo Launched.
) else (
  echo You can launch it later from the Desktop or Start Menu shortcut.
)
echo.
pause
