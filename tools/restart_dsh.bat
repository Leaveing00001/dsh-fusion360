@echo off
chcp 65001 >nul
title DSH Desktop - clean restart
cd /d "%~dp0"

rem Find a Python 3 interpreter, most specific first.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo   No Python 3 interpreter found on PATH.
  echo   Install Python 3.8+ or add it to PATH, then run this again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Restarting DSH Desktop so the Fusion 360 MCP tools load.
echo   Any DSH chat you have open right now will close.
echo.
echo   Starting in 5 seconds - close this window to cancel.
timeout /t 5 /nobreak >nul

%PY% "%~dp0restart_dsh.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo   Restart finished successfully.
) else (
  echo   Restart FAILED ^(exit %RC%^) - read the messages above.
)
echo.
pause
exit /b %RC%
