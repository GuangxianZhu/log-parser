@echo off
rem Double-click to start. You can also drag a log folder onto this file.
cd /d "%~dp0"
python app.py %*
if errorlevel 1 pause
