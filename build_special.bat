@echo off
REM ============================================================
REM build_special.bat -- build ONE "special / trial edition" exe (run on Windows)
REM
REM How the special edition is assembled (all three pieces come together here):
REM   1) Based on the Creator recipe: faster-whisper bundled, so "breakdown"
REM      actually runs (you can only SEE the limits once it can run);
REM   2) Breakdown limits need NO config: SPECIAL_BUILD=True is hardcoded in
REM      core/special.py (DeepSeek forced off + breakdown success capped at 2);
REM   3) Runtime hook _special_trial_hook.py bakes a 72-hour trial into the exe
REM      WITHOUT touching the developer's real core/config_local.py.
REM
REM Output: dist\AIGC-Special.exe   (rename to the Chinese display name after build)
REM Before real distribution: this embeds core\config_local (paid API addresses),
REM   like the Creator build. To hand out safely, switch to: --exclude-module=core.config_local
REM
REM Keep this whole file ASCII: cmd.exe mis-parses UTF-8 batch under GBK codepage.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

if not exist "core\config_local.py" (
    echo [WARN] core\config_local.py NOT found - packaged exe has no API addresses baked in.
)

REM Special build demos breakdown, so faster-whisper is required; abort if missing.
%PY% -c "import faster_whisper" 2>nul
if errorlevel 1 (
    echo [ERROR] faster-whisper NOT installed. Special build needs it for the breakdown demo.
    echo [ERROR] Install first:  .venv\Scripts\pip install -r requirements-breakdown.txt
    goto :error
)

REM ffmpeg.exe baked in (screen recorder + watermark need it at runtime).
set FFMPEG_DATA=
if exist "assets\ffmpeg.exe" set FFMPEG_DATA=--add-data "assets\ffmpeg.exe;assets"

%PY% -m PyInstaller --noconfirm --onefile --noconsole --name "AIGC-Special" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --runtime-hook=_special_trial_hook.py ^
    --hidden-import=openpyxl --hidden-import=core.config_local ^
    --hidden-import=core.trial --hidden-import=core.special ^
    --hidden-import=volcengine.base.Service ^
    --hidden-import=volcengine.ApiInfo ^
    --hidden-import=volcengine.Credentials ^
    --hidden-import=volcengine.ServiceInfo ^
    --collect-all=ctranslate2 --collect-all=faster_whisper ^
    %FFMPEG_DATA% desktop.py || goto :error

echo.
echo ============================================================
echo  SPECIAL BUILD OK. Output: dist\AIGC-Special.exe
echo  Baked: SPECIAL_BUILD=True  (DeepSeek forced off + breakdown capped at 2 successes)
echo  Baked: 72-hour trial via _special_trial_hook.py  (first launch starts the clock)
echo  Trial state file lives under %%APPDATA%%  (delete trial.json to reset the clock)
echo  Breakdown quota file under %%APPDATA%%  (delete special.json to reset the 2-run counter)
echo  NOTE: config_local is embedded. Strip it before handing the exe to others.
goto :eof

:error
echo SPECIAL BUILD FAILED - see the message above
exit /b 1
