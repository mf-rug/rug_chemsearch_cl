@echo off
setlocal enabledelayedexpansion

echo.
echo ============================================
echo   Chemical Extractor - Windows Build Script
echo ============================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

REM Get Python version
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo Found Python %PYVER%

REM Check if we're in the right directory
if not exist "web_app.py" (
    echo ERROR: web_app.py not found.
    echo Please run this script from the project directory.
    pause
    exit /b 1
)

if not exist "extract_chemicals.py" (
    echo ERROR: extract_chemicals.py not found.
    echo Please run this script from the project directory.
    pause
    exit /b 1
)

REM Check for the static data file
if not exist "pubchem_dump_cid_to_cas.tsv.gz" (
    echo ERROR: pubchem_dump_cid_to_cas.tsv.gz not found.
    echo This file is required for the build.
    pause
    exit /b 1
)

echo.
echo Step 1/4: Creating virtual environment...
echo ------------------------------------------

REM Remove existing venv if present
if exist "build_venv" (
    echo Removing existing build_venv...
    rmdir /s /q build_venv
)

python -m venv build_venv
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

echo Virtual environment created.

echo.
echo Step 2/4: Installing dependencies...
echo ------------------------------------------

call build_venv\Scripts\activate.bat

REM Upgrade pip first
python -m pip install --upgrade pip

REM Install all dependencies
pip install beautifulsoup4>=4.12.0 pandas>=2.0.0 requests>=2.31.0 tqdm>=4.66.0 lxml>=5.0.0 aiohttp>=3.9.0 selenium>=4.15.0 flask>=3.0.0

REM Install python-snappy (may fail on some systems - that's OK, it's optional)
echo.
echo Installing python-snappy (optional, may fail on some systems)...
pip install python-snappy>=0.6.0 2>nul
if errorlevel 1 (
    echo WARNING: python-snappy installation failed.
    echo The app will work but Firefox history reading may be slower.
    echo Continuing with build...
)

REM Install PyInstaller
pip install pyinstaller>=6.0.0

echo.
echo Dependencies installed.

echo.
echo Step 3/4: Building executable with PyInstaller...
echo ------------------------------------------

REM Run PyInstaller with spec file
if exist "chemical_extractor.spec" (
    pyinstaller chemical_extractor.spec --noconfirm
) else (
    echo ERROR: chemical_extractor.spec not found.
    pause
    exit /b 1
)

if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo Build complete!

echo.
echo Step 4/4: Cleaning up...
echo ------------------------------------------

REM Deactivate virtual environment
call deactivate 2>nul

echo.
echo ============================================
echo   BUILD SUCCESSFUL!
echo ============================================
echo.
echo Your executable is ready at:
echo   dist\ChemicalExtractor\ChemicalExtractor.exe
echo.
echo To distribute:
echo   1. Zip the entire dist\ChemicalExtractor folder
echo   2. Share the zip file with users
echo.
echo End user requirements:
echo   - Chrome browser (for "Fetch from RUG" feature)
echo   - Firefox (optional, only for Combine feature)
echo.
pause
