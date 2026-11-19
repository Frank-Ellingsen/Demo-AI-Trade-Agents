@echo off
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo Starting OBX Execution Server on http://localhost:8000 ...
start "" "http://localhost:8000/index.html"
python runner_server.py
