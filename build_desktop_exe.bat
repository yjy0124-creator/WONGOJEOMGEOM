@echo off
cd /d "%~dp0"
pyinstaller --onefile --windowed --name TeamAudit ^
  --add-data "team_web;team_web" ^
  --add-data "document.schema.json;." ^
  team_app_desktop.py
