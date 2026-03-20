@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_BIN=python"
for /f "delims=" %%I in ('where python 2^>nul') do if not defined PYTHON_BIN_FULL set "PYTHON_BIN_FULL=%%I"
if defined PYTHON_BIN_FULL set "PYTHON_BIN=%PYTHON_BIN_FULL%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8766/health' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel% equ 0 exit /b 0

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PYTHON_BIN%' -ArgumentList '""%~dp0devicewebui_local_server.py""' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
