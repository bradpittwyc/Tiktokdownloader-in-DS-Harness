@echo off
setlocal
set "HERE=%~dp0"
set "APP=%HERE%outputs\TikTokBatchMVP"

if not exist "%APP%\web_app.py" (
    echo Cannot find "%APP%\web_app.py"
    pause
    exit /b 1
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%APP%\web_app.py"
    goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%APP%\web_app.py"
    goto :eof
)

echo Python not found. Please install Python 3.10+ and add it to PATH.
pause
