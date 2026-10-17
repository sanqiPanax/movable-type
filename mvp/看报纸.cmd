@echo off
rem Open newspaper via local tracking service (starts it if not running).
rem Service auto-picks a free port and opens the browser itself.
cd /d "%~dp0"
python -X utf8 track_server.py --open
if errorlevel 1 (
  echo.
  echo [ERROR] Failed to start tracking service.
  pause
)
