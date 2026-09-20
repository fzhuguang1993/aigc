@echo off
REM ============================================================
REM build.bat —— Windows 一键打包脚本（在 Windows 上运行）
REM 产物：dist\AIGC视频助手.exe（单文件，含 Python 解释器，免安装）
REM ============================================================

pip install -r requirements.txt pyinstaller || goto :error

pyinstaller --onefile --name "AIGC视频助手" ^
    --hidden-import=openpyxl ^
    main.py || goto :error

echo.
echo ============================================================
echo  打包完成: dist\AIGC视频助手.exe
echo  分发给同事时，将以下文件放在同一个文件夹：
echo    AIGC视频助手.exe
echo    AIGC辅助excel.xlsx
echo    material\  （参考图 / KOL 素材）
echo ============================================================
goto :eof

:error
echo 打包失败，请检查上方报错信息
exit /b 1
