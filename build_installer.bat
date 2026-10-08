@echo off
REM ============================================================
REM build_installer.bat -- build the Windows Setup package (run on Windows)
REM
REM Needs two things:
REM   1) the exes under dist\  (run build.bat first)
REM   2) Inno Setup 6 (ISCC.exe). Install it once with either:
REM          winget install JRSoftware.InnoSetup
REM          choco install innosetup -y          (used by the CI workflow)
REM
REM Output: dist\AIGC....-Setup-<version>.exe   (Chinese name comes from the .iss)
REM NOTE: keep every echo in ASCII to avoid cmd.exe
REM mis-parsing UTF-8 batch lines under codepage 65001.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

REM ---------- 1. refuse to build an empty installer ----------
dir /b "dist\*.exe" 2>nul | findstr /i /r "exe" >nul
if errorlevel 1 (
    echo [ERROR] no exe under dist\ - run build.bat first.
    exit /b 1
)

REM ---------- 2. locate ISCC.exe (install path varies) ----------
set ISCC=
where ISCC.exe >nul 2>nul
if not errorlevel 1 set ISCC=ISCC.exe
if not defined ISCC call :find_iscc "%ProgramFiles(x86)%\Inno Setup 6"
if not defined ISCC call :find_iscc "%ProgramFiles%\Inno Setup 6"
if not defined ISCC call :find_iscc "%LOCALAPPDATA%\Programs\Inno Setup 6"
if defined ISCC goto :version

echo [ERROR] ISCC.exe not found - Inno Setup is not installed on this machine.
echo         Install it once, then re-run this script:
echo             winget install JRSoftware.InnoSetup
echo             ^(or: choco install innosetup -y^)
exit /b 1

:find_iscc
if exist "%~1\ISCC.exe" set ISCC=%~1\ISCC.exe
exit /b 0

:version
REM ---------- 3. version: single source is APP_VERSION in core\config.py ----------
REM Quote %PY%: the interpreter is often .venv\Scripts\python.exe, and any
REM project path with a space would otherwise split into two commands.
set VER=
for /f "usebackq delims=" %%V in (`"%PY%" installer\read_version.py`) do set VER=%%V
if not defined VER (
    echo [ERROR] cannot read APP_VERSION from core\config.py
    exit /b 1
)
echo Building Setup package for version %VER% ...
echo Using ISCC: %ISCC%

REM ---------- 4. compile ----------
"%ISCC%" /DMyAppVersion=%VER% installer\aigc.iss
if errorlevel 1 (
    echo [ERROR] ISCC failed - see the compiler messages above.
    exit /b 1
)

echo.
echo ============================================================
echo  SETUP OK - the installer is in dist\ (file name ends with -Setup-%VER%.exe)
echo  It installs into Program Files (admin), while finished videos and the
echo  task database stay in the per-user data folder, so an upgrade or an
echo  uninstall never touches them.
echo ============================================================
goto :eof
