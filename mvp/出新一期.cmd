@echo off
rem Build a new issue: fetch 48h news -> score -> AI editor -> images -> render
cd /d "%~dp0"
python -X utf8 build_paper.py --fetch
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed. See messages above.
  pause
  exit /b 1
)
start "" "%~dp0out\index.html"
