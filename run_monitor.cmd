@echo off
setlocal
cd /d "%~dp0"
python monitor.py --interval 5
endlocal
