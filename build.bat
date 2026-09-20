@echo off
REM ============================================================
REM build.bat —— Windows 一键打包脚本（在 Windows 上运行）
REM 优先使用项目虚拟环境 .venv（和日常开发同一套依赖），
REM 没有 .venv 时自动回退全局 python。
REM 产物：
REM   dist\AIGC视频助手.exe        桌面 GUI 版（发给同事，无黑窗口）
REM   dist\AIGC视频助手-命令行.exe  控制台版（自己调试用，有黑窗口）
REM 两个 exe 共用同一个 data\aigc.db，在哪边跑都一样
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
REM NOTE: keep the echo sections in ASCII to avoid cmd.exe
REM mis-parsing UTF-8 batch lines under codepage 65001.

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

%PY% -m pip install -r requirements.txt pyinstaller || goto :error

%PY% -m PyInstaller --noconfirm --onefile --noconsole --name "AIGC视频助手" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --hidden-import=openpyxl ^
    desktop.py || goto :error

%PY% -m PyInstaller --noconfirm --onefile --console --name "AIGC视频助手-命令行" ^
    --icon=assets\app.ico --add-data "assets\app.ico;assets" ^
    --hidden-import=openpyxl ^
    main.py || goto :error

echo.
echo ============================================================
echo  BUILD OK. Outputs in dist\ :
echo    AIGC*-GUI exe   (no console window)
echo    AIGC* CLI exe   (console version)
echo  To distribute: put the exe together with a material\ folder.
echo  First run opens a setup dialog (name + API addresses),
echo  saved to config.json in the running directory.
echo  Re-configure: delete config.json and start again.
echo ============================================================
goto :eof

:error
echo BUILD FAILED - see the error message above
exit /b 1
