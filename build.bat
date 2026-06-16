@echo off
echo ========================================
echo  DS916 Theme Builder - Build Script
echo ========================================
echo.

:: Install dependencies
echo [1/3] Installing Python dependencies...
pip install pyinstaller pillow pyserial pystray --quiet
if errorlevel 1 (echo ERROR: pip install failed && pause && exit /b 1)
echo Done.
echo.

:: Build exe
echo [2/3] Building DS916Tray.exe...
pyinstaller --onefile --windowed --name=DS916Tray --hidden-import=pystray._win32 --hidden-import=PIL._tkinter_finder ds916_tray.py
if errorlevel 1 (echo ERROR: PyInstaller failed && pause && exit /b 1)
echo Done.
echo.

:: Copy files to dist folder
echo [3/3] Copying theme builder...
copy theme_builder.html dist\theme_builder.html >nul
echo.
echo ========================================
echo  Build complete!
echo  Files in: dist\
echo    - DS916Tray.exe     (run this)
echo    - theme_builder.html (opens in browser)
echo ========================================
echo.
echo Run DS916Tray.exe to start.
echo It will add itself to Windows startup automatically.
pause
