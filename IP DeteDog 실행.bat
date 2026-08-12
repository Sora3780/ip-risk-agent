@echo off
chcp 65001 >nul
cd /d "%~dp0"
python scripts\launch.py
if errorlevel 1 pause
