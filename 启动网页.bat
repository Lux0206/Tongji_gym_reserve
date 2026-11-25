@echo off
cd /d "%~dp0"
start "" pythonw "webapp.py"
if errorlevel 1 start "" python "webapp.py"
