@echo off
rem Copy this file to the Windows desktop. Checks Monarch first, logs in only if it is really down, then re-checks.
title Monarch Login Refresh
wsl.exe -d Ubuntu-24.04 --cd /home/jscherb1/projects/scherbring-family-assistant -- .venv/bin/python scripts/connection_refresh.py monarch
echo.
echo Done. Check the result above, then close this window.
pause
