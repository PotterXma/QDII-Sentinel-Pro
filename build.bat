@echo off
chcp 65001 >nul 2>&1

echo ===================================
echo  QDII Sentinel Pro - Build Script
echo ===================================
echo.

where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [!] Installing pyinstaller...
    pip install pyinstaller
)

echo [*] Building...
pyinstaller --onefile --noconsole --name QDII_Sentinel ^
    --hidden-import pystray._win32 ^
    --hidden-import PIL ^
    --hidden-import PIL._tkinter_finder ^
    main.py

echo [*] Copying config...
if not exist "dist" mkdir dist
copy /Y config.ini dist\

echo.
echo ===================================
echo  Build Complete!
echo  Output: dist\QDII_Sentinel.exe
echo  Config: dist\config.ini
echo ===================================
echo.
echo  NOTE: Place config.ini in the same
echo  folder as QDII_Sentinel.exe
echo ===================================
pause
