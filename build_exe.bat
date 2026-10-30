@echo off
chcp 65001 >nul
rem 単体の exe（dist\AudioTaskbarIndicator.exe）を作ります。
rem 使う Python を変えたいときは、次の行を py -3.12 などに書き換えてください。
set PY=py

cd /d "%~dp0"
%PY% -m pip install -r requirements.txt pyinstaller || goto :error
%PY% -m PyInstaller --noconsole --onefile --name AudioTaskbarIndicator ^
    --hidden-import pystray._win32 audio_taskbar_indicator.pyw || goto :error

echo.
echo 完成: %~dp0dist\AudioTaskbarIndicator.exe
pause
exit /b 0

:error
echo.
echo ビルドに失敗しました。
pause
exit /b 1
