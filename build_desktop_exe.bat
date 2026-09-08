@echo off
cd /d "%~dp0"
pyinstaller --onefile --windowed --name TeamAudit ^
  --add-data "team_web;team_web" ^
  --add-data "document.schema.json;." ^
  --add-data "music_editing_terms.json;." ^
  --add-data "team_data/curricula;team_data/curricula" ^
  --hidden-import ai_activity_adapter ^
  team_app_desktop.py
