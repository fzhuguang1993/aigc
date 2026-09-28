@echo off
REM ============================================================
REM build.bat —— Windows 一键打包脚本（在 Windows 上运行）
REM 优先使用项目虚拟环境 .venv（和日常开发同一套依赖），
REM 没有 .venv 时自动回退全局 python。
REM 产物：
REM   dist\AIGC视频助手.exe            桌面 GUI 精简版（发同事，无黑窗口，不含 whisper，<300MB）
REM   dist\AIGC视频助手-创作版.exe     桌面 GUI 创作版（内置 faster-whisper：录屏字幕/爆款拆解开箱即用）
REM   dist\AIGC视频助手-命令行.exe      控制台版（自己调试用，有黑窗口）
REM 三个 exe 共用同一个 data\aigc.db，在哪边跑都一样。
REM 创作版需要打机 .venv 里装了 faster-whisper；没装则自动跳过（见下方 :skip_creator）。
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

REM 精简版剪掉 whisper 及其编译型大件，守 onefile <300MB；字幕/拆解在精简版里走降级提示。
REM 标准版对外分发：连 core.config_local 一并抹除（收费接口地址/key 不随包外泄），
REM 装好即全新未配置态，首跑走引导自填地址。创作版/命令行版仍内置，方便维护人自用。
%PY% -m PyInstaller --noconfirm --onefile --noconsole --name "AIGC视频助手" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --hidden-import=openpyxl ^
    --hidden-import=volcengine.base.Service ^
    --hidden-import=volcengine.ApiInfo ^
    --hidden-import=volcengine.Credentials ^
    --hidden-import=volcengine.ServiceInfo ^
    --exclude-module=faster_whisper --exclude-module=ctranslate2 ^
    --exclude-module=tokenizers --exclude-module=av ^
    --exclude-module=core.config_local ^
    %FFMPEG_DATA% desktop.py || goto :error

REM ---------- 创作版（内置 faster-whisper） ----------
REM 不排除 whisper 大件；依赖打机 .venv 已装 faster-whisper（装模型/转写都靠它）。
%PY% -c "import faster_whisper" 2>nul
if errorlevel 1 (
    echo [WARN] faster-whisper NOT installed - skipping the Creator build.
    echo [WARN] Install it then re-run:  .venv\Scripts\pip install -r requirements-breakdown.txt
    goto :skip_creator
)
%PY% -m PyInstaller --noconfirm --onefile --noconsole --name "AIGC视频助手-创作版" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --hidden-import=openpyxl --hidden-import=core.config_local ^
    --hidden-import=volcengine.base.Service ^
    --hidden-import=volcengine.ApiInfo ^
    --hidden-import=volcengine.Credentials ^
    --hidden-import=volcengine.ServiceInfo ^
    --collect-all=ctranslate2 --collect-all=faster_whisper ^
    %FFMPEG_DATA% desktop.py || goto :error
:skip_creator

%PY% -m PyInstaller --noconfirm --onefile --console --name "AIGC视频助手-命令行" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --hidden-import=openpyxl --hidden-import=core.config_local ^
    --hidden-import=volcengine.base.Service ^
    --hidden-import=volcengine.ApiInfo ^
    --hidden-import=volcengine.Credentials ^
    --hidden-import=volcengine.ServiceInfo ^
    %FFMPEG_DATA% main.py || goto :error

echo.
echo ============================================================
echo  BUILD OK. Outputs in dist\ :
echo    slim GUI exe       (no console, NO whisper)
echo    creator GUI exe    (whisper built-in: subtitle/breakdown work offline; missing dep = skipped)
echo    console exe        (CLI version)
echo  To distribute: put the exe together with a material\ folder.
echo  First run opens a setup dialog (name + API addresses).
echo  CREDENTIALS (config.json / ui_state.json / api_text) are saved to the
echo  HIDDEN folder  %%APPDATA%%\AIGC视频助手  (NOT next to the exe).
echo  Products (data/, logs/, outputs/, material/) still live next to the exe,
echo  so launching from another folder never creates a second, empty copy.
echo  Re-configure: delete config.json inside %%APPDATA%%\AIGC视频助手 and restart.
echo ============================================================
goto :eof

:error
echo BUILD FAILED - see the error message above
exit /b 1
