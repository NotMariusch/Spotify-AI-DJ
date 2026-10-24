@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

echo Starting SpotifyDJ Dashboard...
echo Open http://127.0.0.1:5001 in your browser.
echo.

".venv\Scripts\python.exe" "src\dj_server.py"

exit /b %errorlevel%
