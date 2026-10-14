@echo off
REM ============================================================
REM build.bat —— Windows 一键打包脚本（在 Windows 上运行）
REM 优先使用项目虚拟环境 .venv（和日常开发同一套依赖），
REM 没有 .venv 时自动回退全局 python。
REM Outputs:
REM   dist\AIGC视频助手.exe            formal GUI exe (for distribution, no console,
REM                                    no whisper, API config stripped)
REM   dist\AIGC视频助手-创作版.exe     creator GUI exe (built-in faster-whisper, maintainer
REM                                    only, keeps ALL API config)
REM Both exes share the same data\aigc.db, no matter which one runs first.
REM Creator build needs faster-whisper in .venv; missing dep = auto skip (:skip_creator).
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
REM NOTE: keep the echo sections in ASCII to avoid cmd.exe
REM mis-parsing UTF-8 batch lines under codepage 65001.

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

REM Local config holds the real API credentials (gitignored). The exe
REM embeds it, so a build machine without this file ships an empty config.
if not exist "core\config_local.py" (
    echo [WARN] core\config_local.py NOT found - the packaged exe will have
    echo [WARN] no API addresses baked in. Copy it from the maintainer first.
)

%PY% -m pip install -r requirements.txt pyinstaller || goto :error

REM ffmpeg.exe is baked into the exe (screen recorder + watermark tools need
REM it at runtime). Maintainer drops it into assets\ locally; not in git.
set FFMPEG_DATA=
if exist "assets\ffmpeg.exe" set FFMPEG_DATA=--add-data "assets\ffmpeg.exe;assets"
if not defined FFMPEG_DATA (
    echo [WARN] assets\ffmpeg.exe NOT found - the packaged exe will have no
    echo [WARN] built-in ffmpeg. Screen recording / watermark then need ffmpeg
    echo [WARN] on the user's PATH. Drop ffmpeg.exe into assets\ and rebuild.
)

REM Lite build cuts whisper + its heavy compiled deps to keep onefile < 300MB;
REM subtitle/breakdown show a graceful degrade hint there.
REM Formal build for distribution: strip core.config_local too (paid API
REM addresses/keys must never ship inside the installer); first run opens the
REM setup dialog. Creator build keeps ALL API config (maintainer only).
REM pypdf MUST be listed explicitly: core/fileindex lazy-imports it inside a
REM function body, PyInstaller static analysis misses it - otherwise local doc
REM search works in dev but silently finds no PDF in the packaged exe.
%PY% -m PyInstaller --noconfirm --onefile --noconsole --name "AIGC视频助手" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --hidden-import=openpyxl --hidden-import=pypdf ^
    --hidden-import=volcengine.base.Service ^
    --hidden-import=volcengine.ApiInfo ^
    --hidden-import=volcengine.Credentials ^
    --hidden-import=volcengine.ServiceInfo ^
    --exclude-module=faster_whisper --exclude-module=ctranslate2 ^
    --exclude-module=tokenizers --exclude-module=av ^
    --exclude-module=core.config_local ^
    %FFMPEG_DATA% desktop.py || goto :error

REM ---------- Creator build (bundled faster-whisper) ----------
REM No whisper exclusion here; needs faster-whisper in the build .venv
REM (model download + transcription both rely on it).
%PY% -c "import faster_whisper" 2>nul
if errorlevel 1 (
    echo [WARN] faster-whisper NOT installed - skipping the Creator build.
    echo [WARN] Install it then re-run:  .venv\Scripts\pip install -r requirements-breakdown.txt
    goto :skip_creator
)
%PY% -m PyInstaller --noconfirm --onefile --noconsole --name "AIGC视频助手-创作版" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --hidden-import=openpyxl --hidden-import=core.config_local --hidden-import=pypdf ^
    --hidden-import=volcengine.base.Service ^
    --hidden-import=volcengine.ApiInfo ^
    --hidden-import=volcengine.Credentials ^
    --hidden-import=volcengine.ServiceInfo ^
    --collect-all=ctranslate2 --collect-all=faster_whisper ^
    %FFMPEG_DATA% desktop.py || goto :error
:skip_creator

REM Both outputs come from the two PyInstaller calls above; the CLI build was
REM dropped by maintainer request.

echo.
echo ============================================================
echo  BUILD OK. Outputs in dist\ :
echo    formal GUI exe     (no console, NO whisper, API config stripped)
echo    creator GUI exe    (whisper built-in; keeps all API config; missing dep = skipped)
echo  To distribute: put the exe together with a material\ folder.
echo  First run opens a setup dialog (name + API addresses).
echo  CREDENTIALS (config.json / ui_state.json / api_text) are saved to the
echo  HIDDEN folder  %%APPDATA%%\AIGC视频助手  (NOT next to the exe).
echo  Products (data/, logs/, outputs/, material/) go to the DATA HOME:
echo    installed exe : %%LOCALAPPDATA%%\AIGC视频助手  (Program Files is not writable)
echo    portable copy : next to the exe (drop a .aigc_portable marker file there)
echo    dev / python  : the repo folder
echo  An old data folder from a previous layout is adopted on first launch, so
echo  upgrading never shows an empty library. See core/paths.py.
echo  Re-configure: delete config.json inside %%APPDATA%%\AIGC视频助手 and restart.
echo ============================================================
goto :eof

:error
echo BUILD FAILED - see the error message above
exit /b 1
