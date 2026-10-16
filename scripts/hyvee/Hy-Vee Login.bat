@echo off
rem Copy this file to the Windows desktop. It is the one way to sign in to Hy-Vee (needs a person for the CAPTCHA).
title Hy-Vee Login Refresh
echo Opening Hy-Vee sign-in. Solve the CAPTCHA in the browser window; you have 5 minutes.
wsl.exe -d Ubuntu-24.04 --cd /home/jscherb1/projects/scherbring-family-assistant -- .venv/bin/python scripts/hyvee/login_test.py --login-only --manual-wait 300
echo.
echo Done. Check the result above, then close this window.
pause
