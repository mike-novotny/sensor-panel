@echo off
echo ========================================
echo  DS916 Theme Builder - Build Script
echo ========================================
echo.

:: Install dependencies
echo [1/4] Installing Python dependencies...
pip install pyinstaller pillow pyserial pystray --quiet
if errorlevel 1 (echo ERROR: pip install failed && pause && exit /b 1)
echo Done.
echo.

:: Build exe
echo [2/4] Building DS916Tray.exe...
python -m PyInstaller --onefile --windowed --name=DS916Tray --hidden-import=pystray._win32 --hidden-import=PIL._tkinter_finder ds916_tray.py
if errorlevel 1 (echo ERROR: PyInstaller failed && pause && exit /b 1)
echo Done.
echo.

:: Install into AppData -- keeps everything (exe, theme builder, config,
:: themes, discovered sensors) together in one place for easy cleanup later,
:: rather than scattering files across wherever the user happened to
:: extract/build them
echo [3/4] Installing to %%APPDATA%%\DS916Tray...
set "INSTALL_DIR=%APPDATA%\DS916Tray"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /Y dist\DS916Tray.exe "%INSTALL_DIR%\DS916Tray.exe" >nul
copy /Y theme_builder.html "%INSTALL_DIR%\theme_builder.html" >nul
echo Done.
echo.

:: Create Desktop and Start Menu shortcuts pointing at the AppData copy
echo [4/4] Creating shortcuts...
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
  echo WARNING: Shortcut creation failed - you can still run DS916Tray.exe directly from:
  echo   %INSTALL_DIR%
) else (
  echo Done.
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
echo  Shortcuts created:
echo    - Desktop: DS916 Tray
echo    - Start Menu: DS916 Tray
echo.
echo  This build folder (dist\, build\, etc.) is no longer needed
echo  and can be deleted - everything now lives in:
echo    %INSTALL_DIR%
echo.
echo Launch DS916 Tray from the Desktop or Start Menu shortcut.
echo It will add itself to Windows startup automatically.
pause
